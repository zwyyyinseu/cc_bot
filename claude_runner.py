"""
Claude CLI 子进程管理 —— 构建 stream-json I/O 命令、启动子进程、解析 JSONL 输出。

支持两种模式：
1. 单轮模式（message_queue=None）：写 prompt → 关闭 stdin → 读到进程退出
2. 多轮模式（message_queue=asyncio.Queue）：保持 stdin 打开，通过队列接收后续消息，
   消息到达时立即写入 stdin（不等 result），result 后若队列空则关闭。

多轮模式架构（参照 remote_cli/core/claude_process.py 双协程模式）：
  - _stdin_writer: 消息一到立即写入 stdin，减少轮次间延迟
  - _stdin_close_checker: 每收到 result 检查队列，空则通知 writer 关闭
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Callable, Awaitable, Optional

from config import config

# 回调类型
EventCallback = Callable[[dict], Awaitable[None]]
ProcCallback = Callable[[asyncio.subprocess.Process], None]


def _find_claude_bin() -> str:
    """解析 claude 可执行文件路径，兼容 PATH 不完整的情况。"""
    found = shutil.which("claude")
    if found:
        return found
    common = [
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/usr/bin/claude"),
    ]
    for p in common:
        if p.exists():
            return str(p)
    return "claude"


def build_cmd(session_id: Optional[str], claude_bin: Optional[str] = None) -> list[str]:
    """构建 claude CLI 命令行参数列表。"""
    bin_path = claude_bin or config.CLAUDE_BIN
    resume_args = ["--resume", session_id] if session_id else []
    return [
        bin_path, "-p",
        *resume_args,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]


def build_stream_json_input(text: str) -> bytes:
    """构建 stream-json 格式的 stdin 输入（用户消息）。"""
    msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}]
        }
    }
    return (json.dumps(msg) + "\n").encode()


def build_tool_result_input(tool_use_id: str, content: str) -> bytes:
    """构建 tool_result 格式的 stdin 消息（用于回答 AskUserQuestion 等）。"""
    msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]
        }
    }
    return (json.dumps(msg) + "\n").encode()


async def run_claude(
    prompt: str,
    cwd: str,
    session_id: Optional[str] = None,
    on_event: Optional[EventCallback] = None,
    on_proc: Optional[ProcCallback] = None,
    message_queue: Optional[asyncio.Queue] = None,
    on_stdin_close: Optional[Callable[[], None]] = None,
) -> tuple[int, Optional[float]]:
    """
    运行 claude -p，流式解析 stream-json 输出。

    Args:
        prompt: 用户输入文本（第一条消息）
        cwd: Claude 工作目录
        session_id: --resume 会话 ID，None 时新建会话
        on_event: 每个解析事件的异步回调
        on_proc: 进程启动后的回调（用于外部注册进程以支持 kill）
        message_queue: 多轮消息队列，None 时单轮模式
        on_stdin_close: stdin 关闭时的回调，通知外部取消队列注册

    Returns:
        (exit_code, cost_usd)
    """
    cmd = build_cmd(session_id)

    env = os.environ.copy()
    env["IS_SANDBOX"] = "1"
    env.pop("CLAUDECODE", None)  # 避免嵌套检测

    # 启动子进程（重试 3 次，兼容 Python 3.8 竞态）
    last_exc: Exception | None = None
    proc: asyncio.subprocess.Process | None = None
    for attempt in range(3):
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                limit=10 * 1024 * 1024,
            )
            last_exc = None
            break
        except (TypeError, OSError) as e:
            last_exc = e
            print(f"[claude_runner] create_subprocess_exec attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                await asyncio.sleep(0.5)
    if last_exc is not None:
        raise last_exc
    assert proc is not None

    # 通知外部注册此进程
    if on_proc:
        on_proc(proc)

    cost_usd: Optional[float] = None
    # 每轮 result 消息后触发，_stdin_close_checker 等待此事件
    result_event = asyncio.Event()

    # ── 写入第一条消息到 stdin ──────────────────────────────────────────
    try:
        proc.stdin.write(build_stream_json_input(prompt))
        await proc.stdin.drain()
        if message_queue is None:
            # 单轮模式：写完立即关闭 stdin
            proc.stdin.close()
    except Exception as e:
        print(f"[claude_runner] stdin write error: {e}")

    # ── 读取 stdout ────────────────────────────────────────────────────
    async def _read_stdout():
        nonlocal cost_usd
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                if on_event:
                    try:
                        await on_event({"type": "raw", "text": line})
                    except Exception:
                        pass
                continue

            msg_type = obj.get("type", "")
            try:
                if msg_type == "assistant":
                    content = obj.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if text and on_event:
                                await on_event({"type": "text", "text": text})
                        elif block.get("type") == "tool_use":
                            if on_event:
                                await on_event({
                                    "type": "tool_call",
                                    "id": block.get("id", ""),
                                    "name": block.get("name", ""),
                                    "input": block.get("input", {}),
                                })

                elif msg_type == "user":
                    content = obj.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "tool_result":
                            raw_content = block.get("content", "")
                            if isinstance(raw_content, list):
                                text_parts = [c.get("text", "") for c in raw_content
                                              if c.get("type") == "text"]
                                content_str = "\n".join(text_parts)
                            else:
                                content_str = str(raw_content)
                            if on_event:
                                await on_event({
                                    "type": "tool_result",
                                    "tool_use_id": block.get("tool_use_id", ""),
                                    "content": content_str,
                                    "is_error": block.get("is_error", False),
                                })

                elif msg_type == "result":
                    cost_usd = obj.get("cost_usd")
                    if on_event:
                        await on_event({
                            "type": "result",
                            "cost_usd": cost_usd,
                            "duration_ms": obj.get("duration_ms"),
                        })
                    # 通知 _stdin_close_checker 当前轮次已结束
                    result_event.set()

                elif msg_type == "system":
                    if obj.get("subtype") == "init":
                        sid = obj.get("sessionId") or obj.get("session_id")
                        if sid and on_event:
                            await on_event({"type": "session_init", "session_id": sid})
                    text = obj.get("message", "")
                    if text and on_event:
                        await on_event({"type": "system", "text": str(text)})

            except Exception as e:
                print(f"[claude_runner] on_event error: {e}")

    # ── 读取 stderr ────────────────────────────────────────────────────
    async def _read_stderr():
        try:
            async for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").strip()
                if line and on_event:
                    try:
                        await on_event({"type": "stderr", "text": line})
                    except Exception:
                        pass
        except Exception as e:
            print(f"[claude_runner] stderr read error: {e}")

    # ── 多轮消息处理（参照 remote_cli 双协程模式）─────────────────────
    # _stdin_writer: 消息到达时立即写入 stdin（不等待 result），减少轮次间延迟
    # _stdin_close_checker: 每收到 result 后检查队列，空则通知 writer 关闭
    async def _stdin_writer():
        """消息一到立即写入 stdin（不等 result）。"""
        if message_queue is None:
            return
        while True:
            next_msg = await message_queue.get()
            if next_msg is None:  # sentinel：关闭 stdin
                print(f"[claude_runner] closing stdin (sentinel from close_checker)")
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                return

            print(f"[claude_runner] writing message to stdin: type={next_msg.get('msg_type', 'user_message')}")
            # ★ 先通知 stream_handler 创建新卡片，再写入 stdin ★
            # 避免 _read_stdout 读到输出后 append buf，然后 new_round 才 clear buf 的竞态
            if on_event:
                try:
                    await on_event({
                        "type": "new_round",
                        "user_message_id": next_msg.get("user_message_id", ""),
                        "text": next_msg.get("text", ""),
                    })
                except Exception:
                    pass

            try:
                msg_type = next_msg.get("msg_type", "user_message")
                if msg_type == "tool_result":
                    data = build_tool_result_input(
                        next_msg["tool_use_id"], next_msg.get("content", "")
                    )
                else:
                    data = build_stream_json_input(next_msg["text"])
                proc.stdin.write(data)
                await proc.stdin.drain()
            except Exception as e:
                print(f"[claude_runner] stdin write error: {e}")
                return

    async def _stdin_close_checker():
        """每收到一个 result 事件，若队列为空则发送 sentinel 关闭 stdin。
        增加超时保护：若单轮超过 RESULT_TIMEOUT 秒无 result，判定为网络异常/挂死，
        通知上层显示错误卡片并关闭进程。"""
        if message_queue is None:
            return
        RESULT_TIMEOUT = 600.0  # 单轮最长等待 10 分钟（断网时 API 自身超时约 10 分钟）
        while True:
            try:
                await asyncio.wait_for(result_event.wait(), timeout=RESULT_TIMEOUT)
            except asyncio.TimeoutError:
                print(f"[claude_runner] result timeout ({RESULT_TIMEOUT}s), possible network issue")
                if on_event:
                    try:
                        await on_event({"type": "timeout_error", "seconds": RESULT_TIMEOUT})
                    except Exception:
                        pass
                # 关闭 stdin → Claude 进程退出 → stream_handler finally 发错误卡片
                if on_stdin_close:
                    try:
                        on_stdin_close()
                    except Exception:
                        pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                return
            result_event.clear()
            if message_queue.empty():
                print(f"[claude_runner] queue empty after result, closing stdin")
                if on_stdin_close:
                    try:
                        on_stdin_close()
                    except Exception:
                        pass
                await message_queue.put(None)  # sentinel 通知 _stdin_writer
                return
            # 队列还有消息，继续等下一个 result
            print(f"[claude_runner] queue not empty after result, continuing")

    # ── 并发启动所有任务 ───────────────────────────────────────────────
    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())
    writer_task = asyncio.create_task(_stdin_writer()) if message_queue is not None else None
    closer_task = asyncio.create_task(_stdin_close_checker()) if message_queue is not None else None

    # 等待主进程退出
    exit_code = await proc.wait()

    # 排空剩余输出（5s 超时）
    tasks_to_wait = {stdout_task, stderr_task}
    if writer_task is not None:
        tasks_to_wait.add(writer_task)
    if closer_task is not None:
        tasks_to_wait.add(closer_task)

    done, pending = await asyncio.wait(tasks_to_wait, timeout=5.0)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    # 关闭 subprocess transport，释放 fd
    try:
        proc._transport.close()
    except Exception:
        pass
    await asyncio.sleep(0)

    return exit_code, cost_usd
