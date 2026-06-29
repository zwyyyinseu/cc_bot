"""
Claude 输出 → 飞书卡片流式推送。

核心改进：
  - 多轮消息模式：Claude 进程保持 stdin 打开，用户发新消息时通过 queue 写入 stdin
  - 用户发新消息不会 kill 当前 Claude 进程，而是排队继续
  - 只有 /stop 和 /switch 才会终止进程
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from config import config
from feishu_client import FeishuClient
from claude_runner import run_claude
from conversations import conv_store
from history_store import history_store
import logging
log = logging.getLogger(__name__)

if TYPE_CHECKING:
    import asyncio.subprocess


def _tool_desc(name: str, inp: dict) -> str:
    """生成工具调用的简短描述。"""
    desc = inp.get("description", "")
    if desc:
        return desc[:60]
    if name == "Bash":
        return inp.get("command", "")[:60]
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        path = inp.get("file_path", inp.get("notebook_path", ""))
        return path.split("/")[-1] if path else ""
    if name in ("Glob", "Grep"):
        return inp.get("pattern", "")[:40]
    return ""


def _truncate_for_card(text: str, max_chars: int = config.CARD_MAX_CHARS,
                      workspace: str = "") -> tuple[str, Optional[str]]:
    """截断文本以适应飞书卡片大小限制。返回 (截断后文本, 完整文件路径或None)。"""
    if len(text) <= max_chars:
        return text, None
    # 保存完整输出到文件
    saved_path = ""
    if workspace:
        ts = time.strftime("%m%d-%H%M%S")
        out_file = Path(workspace) / f"output_{ts}.md"
        try:
            out_file.write_text(text, encoding="utf-8")
            saved_path = str(out_file)
            log.info("full output saved to %s", saved_path)
        except Exception as e:
            log.error("failed to save full output: %s", e)
    truncated = text[:max_chars - 80] + f"\n\n---\n📄 完整输出已保存: `{saved_path}`" if saved_path else text[:max_chars - 20] + "\n\n...(内容过长，已截断)"
    return truncated, saved_path


async def run_claude_and_stream(
    feishu: FeishuClient,
    text: str,
    thinking_msg_id: str,
    user_message_id: str,
    session_id: Optional[str],
    workspace: str,
    on_proc: "callable",
    message_queue: Optional[asyncio.Queue] = None,
    on_stdin_close: Optional["callable"] = None,
) -> None:
    """
    运行 Claude 并流式回传结果到飞书卡片。

    多轮模式：传入 message_queue 时，Claude 进程不会自动关闭 stdin，
    用户发新消息通过 queue 写入，实现连续对话。
    """
    buf: list[str] = []                  # 累积当前卡片的文本输出
    tool_log: list[str] = []             # 当前卡片的工具调用记录
    history_buf: list[str] = []          # 累积完整回复文本（不受 tool_call 清空 buf 影响）
    current_msg_id = [thinking_msg_id]   # 当前正在更新的卡片 ID
    text_started = [False]               # 当前卡片是否已有文本输出
    last_msg_update = [0.0]              # 最后一次卡片更新时间（节流）
    start_time = [time.monotonic()]      # 当前轮次的开始时间（new_round 时重置）
    got_result = [False]                 # 是否收到了 result 事件
    last_output_time = [time.monotonic()]  # 最后一次收到输出的时间（用于检测断网）

    async def _throttled_update(msg_id: str, content: str, force: bool = False) -> None:
        """节流更新：中间过程最少间隔 THROTTLE_INTERVAL 秒，force=True 时强制发送。"""
        now = time.monotonic()
        if force or now - last_msg_update[0] >= config.THROTTLE_INTERVAL:
            last_msg_update[0] = now
            truncated, saved_path = _truncate_for_card(content, workspace=workspace)
            card_json = FeishuClient.build_card(truncated)
            # 尝试更新，失败时重试一次（可能是 token 过期等瞬时错误）
            success = await feishu.update_card(msg_id, card_json)
            if not success:
                # 重试一次
                await asyncio.sleep(1.0)
                success = await feishu.update_card(msg_id, card_json)
            if not success:
                log.error(f"update_card failed for msg_id={msg_id[:20]}..., force={force}")
                if force:
                    new_id = await feishu.reply_message(user_message_id, card_json)
                    if new_id:
                        current_msg_id[0] = new_id
                        last_msg_update[0] = time.monotonic()
                    else:
                        log.error(f"fallback reply_message also failed")
            # 文件截断保存时，上传到飞书让用户可在手机端打开
            if saved_path and force:
                file_key = await feishu.upload_file(saved_path)
                if file_key:
                    await feishu.reply_file(user_message_id, file_key)
                    log.info("file sent to chat: %s", saved_path)

    # ── 思考计时器 ──────────────────────────────────────────────────────
    async def _thinking_ticker():
        """无输出时每 5s 刷新状态，根据等待时长给出不同提示。
        - < 60s: 正常等待
        - 60-120s: 提示复杂任务
        - > 120s: 提示可能网络异常
        """
        icons = ["⏳", "🤔"]
        i = 0
        while True:
            await asyncio.sleep(5.0)
            if not tool_log and not buf and not text_started[0]:
                elapsed = int(time.monotonic() - start_time[0])
                no_output_sec = int(time.monotonic() - last_output_time[0])
                if no_output_sec > 120:
                    hint = "⚠️ 长时间无响应，可能网络异常，等待 API 超时后重试..."
                elif elapsed > 60:
                    hint = f"{icons[i % 2]} 正在思考... ({elapsed}s) — 复杂任务处理中"
                else:
                    hint = f"{icons[i % 2]} 正在思考... ({elapsed}s)"
                await _throttled_update(current_msg_id[0], hint)
                i += 1

    ticker = asyncio.create_task(_thinking_ticker())

    # ── 事件回调 ────────────────────────────────────────────────────────
    async def on_event(entry: dict) -> None:
        entry_type = entry.get("type")

        if entry_type == "new_round":
            # 多轮对话：新消息来了，创建新卡片（不覆盖上一轮的卡片）
            new_user_msg_id = entry.get("user_message_id", "")
            new_card = None
            if new_user_msg_id:
                new_card = await feishu.reply_message(
                    new_user_msg_id,
                    FeishuClient.build_card("⏳ 正在思考...")
                )
            # 如果 reply_message 失败，用原始 user_message_id 作为 fallback
            if not new_card:
                log.error(f"new_round: reply_message failed, using fallback")
                new_card = await feishu.reply_message(
                    user_message_id,
                    FeishuClient.build_card("⏳ 正在处理新消息...")
                )
            # 只有成功创建新卡片后，才切换 current_msg_id 和清空缓冲区
            if new_card:
                current_msg_id[0] = new_card
                last_msg_update[0] = time.monotonic()
                start_time[0] = time.monotonic()  # 重置思考计时器
                last_output_time[0] = time.monotonic()  # 重置无输出检测
                buf.clear()
                tool_log.clear()
                history_buf.clear()  # 新一轮对话，重置历史缓冲
                text_started[0] = False
                got_result[0] = False
            else:
                log.error(f"new_round: FAILED to create card, keeping old card")

        elif entry_type == "session_init":
            new_sid = entry.get("session_id")
            if new_sid:
                active = conv_store.active
                if active:
                    conv_store.update(active.id, session_id=new_sid)
                    log.info(f"session_id saved to conv {active.id}: {new_sid}")

        elif entry_type == "text":
            chunk = entry.get("text", "")
            if not chunk:
                return
            last_output_time[0] = time.monotonic()
            buf.append(chunk)
            history_buf.append(chunk)  # 累积完整回复（不受 tool_call 清空影响）
            text_started[0] = True
            await _throttled_update(current_msg_id[0], "".join(buf))

        elif entry_type == "tool_call":
            last_output_time[0] = time.monotonic()
            name = entry.get("name", "")
            desc = _tool_desc(name, entry.get("input", {}))
            line = f"🔧 {name}: {desc} 执行中" if desc else f"🔧 {name} 执行中"

            if text_started[0]:
                final_text = "".join(buf)
                if final_text:
                    await _throttled_update(current_msg_id[0], final_text, force=True)
                new_id = await feishu.reply_message(
                    user_message_id,
                    FeishuClient.build_card(line)
                )
                if new_id:
                    current_msg_id[0] = new_id
                    last_msg_update[0] = time.monotonic()
                buf.clear()
                tool_log.clear()
                tool_log.append(line)
                text_started[0] = False
            else:
                tool_log.append(line)
                await _throttled_update(current_msg_id[0], "\n".join(tool_log))

        elif entry_type == "tool_result":
            last_output_time[0] = time.monotonic()
            if tool_log and "执行中" in tool_log[-1]:
                tool_log[-1] = tool_log[-1].replace("执行中", "结果分析中")
                if not text_started[0]:
                    await _throttled_update(current_msg_id[0], "\n".join(tool_log))

        elif entry_type == "result":
            got_result[0] = True
            # 保存 Claude 完整回复到历史
            full_response = "".join(history_buf)
            if full_response:
                active = conv_store.active
                if active:
                    history_store.append(active.id, "assistant", full_response)
            history_buf.clear()
            # result 后重置缓冲区，准备下一轮
            final_text = "".join(buf)
            if final_text:
                await _throttled_update(current_msg_id[0], final_text, force=True)
            elif not tool_log and not text_started[0]:
                await _throttled_update(current_msg_id[0], "✅ 任务完成", force=True)
            # 推送通知：回复新消息触发手机推送（卡片更新不触发推送）
            cost_info = entry.get("cost_usd", "")
            cost_str = f" · ${cost_info:.4f}" if cost_info else ""
            await feishu.reply_message(
                user_message_id,
                FeishuClient.build_card(f"✅ 任务完成{cost_str}")
            )
            buf.clear()
            tool_log.clear()
            text_started[0] = False

        elif entry_type == "stderr":
            # Claude stderr → 记录日志；检测网络错误关键词
            text = entry.get("text", "")
            if text:
                log.info(f"claude stderr: {text[:200]}")
            # 检测网络相关错误
            if text:
                net_keywords = ["ETIMEDOUT", "ECONNREFUSED", "ECONNRESET", "ENOTFOUND",
                               "Network", "network", "timeout", "Timeout", "connect",
                               "DNS", "TLS", "SSL", "socket", "getaddrinfo"]
                if any(kw in text for kw in net_keywords):
                    log.error(f"⚠️ network error detected in stderr!")
                    got_result[0] = True  # 阻止 finally 再发（无输出）卡片
                    await _throttled_update(
                        current_msg_id[0],
                        f"🔴 **网络异常**\n\nClaude 进程报告网络错误：\n```\n{text[:300]}\n```\n\n请检查服务器网络连接。",
                        force=True,
                    )

        elif entry_type == "timeout_error":
            # _stdin_close_checker 超时：Claude 超过 10 分钟无 result
            sec = entry.get("seconds", 600)
            mins = int(sec / 60)
            log.error(f"TIMEOUT: no result for {sec}s, likely network issue or API hang")
            got_result[0] = True  # 阻止 finally 再发卡片
            await _throttled_update(
                current_msg_id[0],
                f"🔴 **响应超时**（>{mins} 分钟）\n\n"
                f"Claude 长时间未返回结果，**可能原因：**\n"
                f"• 服务器网络断开或不稳定\n"
                f"• Anthropic API 连接异常\n"
                f"• Claude 进程卡死\n\n"
                f"💡 请检查服务器网络后重试。当前对话上下文已保留。",
                force=True,
            )

        elif entry_type == "system":
            # system 消息（非 init）记录到日志
            text = entry.get("text", "")
            if text:
                log.info(f"claude system: {text[:200]}")

    log.info(f"claude START: {text[:50]!r}")
    try:
        await run_claude(
            prompt=text,
            cwd=workspace,
            session_id=session_id,
            on_event=on_event,
            on_proc=on_proc,
            message_queue=message_queue,
            on_stdin_close=on_stdin_close,
        )
    except asyncio.CancelledError:
        log.warning("claude CANCELLED")
    except Exception as e:
        buf.append(f"\n\n❌ 执行出错：{e}")
        log.error(f"claude ERROR: {e}")
    finally:
        log.info(f"claude DONE, buf_len={len(buf)}")
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass

        # 最终刷新（仅在没有收到 result 的情况下，收到 result 时已在 on_event 中刷新）
        if not got_result[0]:
            final_text = "".join(buf)
            if final_text:
                await _throttled_update(current_msg_id[0], final_text, force=True)
            elif not tool_log and not text_started[0]:
                await _throttled_update(current_msg_id[0], "（无输出）", force=True)

        on_proc(None)

