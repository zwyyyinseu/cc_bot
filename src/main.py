"""
cc_bot 入口 —— asyncio 事件循环 + 消息轮询主循环。

核心改进：
  - 多轮消息模式：Claude 进程保持 stdin 打开，用户发新消息通过 queue 写入 stdin
  - 用户发新消息不会 kill 当前 Claude 进程，而是排队继续
  - 只有 /stop 和 /switch 才会终止进程
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

from config import config
from state import state_store
from feishu_client import FeishuClient
from conversations import conv_store
from stream_handler import run_claude_and_stream
from history_store import history_store
import logging
log = logging.getLogger(__name__)


class Bot:
    def __init__(self) -> None:
        self.feishu = FeishuClient()
        self._claude_task: Optional[asyncio.Task] = None
        self._claude_proc: Optional[asyncio.subprocess.Process] = None
        self._run_lock = asyncio.Lock()
        # 多轮消息队列：Claude 进程运行期间，新消息写入此队列
        self._message_queue: Optional[asyncio.Queue] = None
        # 对话 ID → message_queue 的映射，用于跨消息保持
        self._conv_queue: dict[str, asyncio.Queue] = {}
        # 休眠/唤醒状态机
        self._idle = True                # 启动默认休眠
        self._last_active = time.monotonic()  # 最后一次用户交互时间
        self._auto_idle_sec = config.AUTO_IDLE_SEC  # 从配置读取

    # ── Claude 进程生命周期 ─────────────────────────────────────────────

    async def stop_claude(self) -> None:
        """终止正在运行的 Claude 进程。"""
        # 清理消息队列
        if self._message_queue:
            try:
                self._message_queue.put_nowait(None)  # sentinel
            except Exception:
                pass
            self._message_queue = None

        if self._claude_proc and self._claude_proc.returncode is None:
            try:
                self._claude_proc.kill()
            except Exception:
                pass
        if self._claude_task and not self._claude_task.done():
            self._claude_task.cancel()
            try:
                await asyncio.shield(self._claude_task)
            except (asyncio.CancelledError, Exception):
                pass
        self._claude_proc = None
        self._claude_task = None
        self._conv_queue.clear()

    def _register_proc(self, proc) -> None:
        """注册 Claude 子进程引用。"""
        self._claude_proc = proc

    def _on_stdin_close(self) -> None:
        """stdin 关闭回调：清理队列注册。"""
        self._message_queue = None
        self._conv_queue.clear()
        log.info("stdin closed, queue unregistered")

    def _is_claude_running(self) -> bool:
        """检查 Claude 进程是否正在运行。"""
        return (self._claude_task is not None
                and not self._claude_task.done()
                and self._claude_proc is not None
                and self._claude_proc.returncode is None)

    def _is_claude_running_for_conv(self, conv_id: str) -> bool:
        """检查指定对话的 Claude 进程是否正在运行。"""
        return self._is_claude_running() and conv_id in self._conv_queue

    # ── Chat ID 发现 ────────────────────────────────────────────────────

    async def _discover_chat_id(self) -> None:
        """向用户发送 P2P 消息以发现 chat_id。"""
        log.info("discovering chat_id by sending P2P message to user...")
        card = FeishuClient.build_card(
            "😴 **Claude Bot 已就绪（休眠中）**\n\n"
            "发送 **`/start`** 激活 Bot，即可开始对话！\n\n"
            "**命令速查：**\n"
            "• `/start` — 唤醒 Bot\n"
            "• `/new 标题` — 创建新对话\n"
            "• `/list` — 查看所有对话\n"
            "• `/switch 序号` — 切换对话\n"
            "• `/history` — 查看历史\n"
            "• `/stop` — 休眠 Bot"
        )
        msg_id = await self.feishu.send_message(
            receive_id=config.FEISHU_OPEN_ID,
            card_json=card,
            receive_id_type="open_id",
        )
        if msg_id:
            log.info(f"online notification sent: {msg_id}")

    # ── 命令处理 ────────────────────────────────────────────────────────

    async def _cmd_list(self, msg_id: str) -> None:
        """列出所有对话。"""
        await self.feishu.reply_message(
            msg_id,
            FeishuClient.build_card(conv_store.format_list())
        )

    async def _cmd_new(self, msg_id: str, title: str = "新对话") -> None:
        """创建新对话并切换。"""
        await self.stop_claude()
        conv = conv_store.create(title=title)
        state_store.update(active_conv_id=conv.id)
        await self.feishu.reply_message(
            msg_id,
            FeishuClient.build_card(f"🆕 已创建并切换到对话「**{conv.title}**」（{conv.id}）\n\n下次对话将从零开始。")
        )

    async def _cmd_rename(self, msg_id: str, new_title: str) -> None:
        """重命名当前活跃对话。"""
        if not new_title.strip():
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card("❌ 请指定新名字，如 `/rename 我的项目`")
            )
            return
        active = conv_store.active
        if not active:
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card("❌ 没有活跃对话")
            )
            return
        old_title = active.title
        conv_store.update(active.id, title=new_title.strip())
        await self.feishu.reply_message(
            msg_id,
            FeishuClient.build_card(f"✏️ 已重命名「**{old_title}**」→「**{new_title.strip()}**」")
        )

    async def _cmd_switch(self, msg_id: str, index_str: str) -> None:
        """切换到指定对话。"""
        try:
            index = int(index_str)
        except ValueError:
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card("❌ 序号必须是数字，如 `/switch 1`")
            )
            return

        conv = conv_store.get_by_index(index)
        if not conv:
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card(f"❌ 序号 {index} 不存在，发送 `/list` 查看所有对话")
            )
            return

        await self.stop_claude()
        conv_store.set_active(conv.id)
        state_store.update(active_conv_id=conv.id)

        # 切换后自动导入 Claude session 历史（如果本地还没有）
        if conv.session_id:
            imported = history_store.import_from_claude(conv.id, conv.workspace, conv.session_id)
            if imported:
                log.info(f"switch: imported {imported} history entries for conv {conv.id}")

        sid_status = f"（有历史上下文）" if conv.session_id else "（新会话）"
        await self.feishu.reply_message(
            msg_id,
            FeishuClient.build_card(
                f"✅ 已切换到对话「**{conv.title}**」（{conv.id}）{sid_status}\n\n"
                f"继续上次的话题吧！"
            )
        )

    async def _cmd_del(self, msg_id: str, index_str: str) -> None:
        """删除指定对话。"""
        try:
            index = int(index_str)
        except ValueError:
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card("❌ 序号必须是数字，如 `/del 1`")
            )
            return

        conv = conv_store.get_by_index(index)
        if not conv:
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card(f"❌ 序号 {index} 不存在，发送 `/list` 查看所有对话")
            )
            return

        conv_title = conv.title
        conv_id = conv.id
        is_active = conv.id == conv_store.active_id

        await self.stop_claude()
        conv_store.delete(conv_id)

        # 同步清理历史记录
        history_store.delete(conv_id)

        active = conv_store.active
        state_store.update(active_conv_id=active.id if active else None)

        active_note = "\n\n⚠️ 删除的是当前活跃对话，已自动切换到最近的对话。" if is_active else ""
        await self.feishu.reply_message(
            msg_id,
            FeishuClient.build_card(f"🗑️ 已删除对话「**{conv_title}**」{active_note}")
        )

    async def _cmd_stop(self, msg_id: str) -> None:
        """终止当前 Claude 进程，进入休眠模式。"""
        if self._is_claude_running():
            await self.stop_claude()
            self._idle = True
            log.info("STOPPED + entering idle")
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card("😴 **Bot 已休眠**\n\nClaude 进程已终止，发送 **`/start`** 唤醒。")
            )
        else:
            self._idle = True
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card("😴 **Bot 已休眠**\n\n发送 **`/start`** 唤醒。")
            )

    async def _cmd_status(self, msg_id: str) -> None:
        """显示 bot 和 Claude 运行状态。"""
        claude_running = self._is_claude_running()
        active = conv_store.active
        active_title = active.title if active else "无"

        mode_line = "😴 休眠中" if self._idle else "🟢 活跃"
        poll_interval = config.IDLE_POLL_INTERVAL if self._idle else config.POLL_INTERVAL

        lines = [
            "📊 **Bot 状态**",
            "",
            f"• 运行模式: {mode_line}",
            f"• Claude 进程: {'🟢 运行中' if claude_running else '⚫ 空闲'}",
            f"• 活跃对话: **{active_title}**",
            f"• 对话总数: {len(conv_store.list_all())}",
            f"• 轮询间隔: {poll_interval}s",
        ]
        if self._idle:
            lines.append("\n💡 发送 **`/start`** 唤醒 Bot")
        else:
            lines.append("\n💡 发送 `/help` 查看所有命令")
        await self.feishu.reply_message(
            msg_id,
            FeishuClient.build_card("\n".join(lines))
        )

    async def _cmd_help(self, msg_id: str) -> None:
        """显示帮助信息。"""
        lines = [
            "📖 **命令列表**",
            "",
            "**对话管理**",
            "• `/new 标题` — 创建新对话",
            "• `/rename 新名字` — 重命名当前对话",
            "• `/list` — 列出所有对话",
            "• `/switch 序号` — 切换对话",
            "• `/del 序号` — 删除对话",
            "",
            "**任务控制**",
            "• `/stop` — 终止当前 Claude 任务",
            "• `/history` — 查看当前对话历史 (可加数字如 `/history 5`)",
            "• `/status` — 查看 Bot 状态",
            "• `/help` — 显示此帮助",
            "",
            "**使用方式**",
            "• 直接发消息与 Claude 对话",
            "• Claude 正在执行时发新消息会自动排队",
            "• 会话上下文自动保留，`/switch` 切换对话继续聊",
        ]
        await self.feishu.reply_message(
            msg_id,
            FeishuClient.build_card("\n".join(lines))
        )

    async def _cmd_history(self, msg_id: str, n: int = config.HISTORY_DISPLAY_N) -> None:
        """显示最近 N 条对话历史。"""
        active = conv_store.active
        if not active:
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card("📭 没有活跃对话")
            )
            return

        # 若本地无历史，尝试从 Claude session 文件导入
        if history_store.count(active.id) == 0 and active.session_id:
            imported = history_store.import_from_claude(active.id, active.workspace, active.session_id)
            if imported:
                log.info(f"imported {imported} history entries for conv {active.id}")

        entries = history_store.get_recent(active.id, n=n)
        total = history_store.count(active.id)

        if not entries:
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card(f"📭 **{active.title}** 暂无历史消息")
            )
            return

        # 构建美化卡片
        lines = [f"📜 **{active.title}** · {total} 条记录 · 最近 {len(entries)} 条\n"]
        for i, entry in enumerate(entries):
            role = entry.get("role", "")
            text = entry.get("text", "")
            ts = entry.get("timestamp", "")
            # 解析时间戳，只显示 MM-DD HH:MM
            time_str = ""
            if ts:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(ts)
                    time_str = f" `{dt.strftime('%m-%d %H:%M')}`"
                except Exception:
                    pass

            # 每条消息截断
            if len(text) > config.HISTORY_MAX_CHARS:
                text = text[:config.HISTORY_MAX_CHARS].rstrip() + "..."

            if role == "user":
                lines.append(f"**🙋 你**{time_str}\n> {text}\n")
            elif role == "assistant":
                lines.append(f"**🤖 Claude**{time_str}\n> {text}\n")

            # 在相邻的用户消息前加分隔线（非首条）
            if i < len(entries) - 1 and entries[i + 1].get("role") == "user":
                lines.append("---\n")

        await self.feishu.reply_message(
            msg_id,
            FeishuClient.build_card("\n".join(lines))
        )

    async def _route_command(self, stripped: str, msg_id: str) -> bool:
        """命令路由表。返回 True 表示已处理（调用方应 return），False 表示非命令。"""
        # ── 无参数命令（精确匹配） ──────────────────────────────────────
        EXACT = {
            "/list": self._cmd_list,
            "/ls": self._cmd_list,
            "/stop": self._cmd_stop,
            "/start": self._cmd_start,
            "/status": self._cmd_status,
            "/stat": self._cmd_status,
            "/help": self._cmd_help,
            "/h": self._cmd_help,
            "/?": self._cmd_help,
        }
        handler = EXACT.get(stripped)
        if handler:
            await handler(msg_id)
            return True

        # ── 带参数命令（前缀匹配 + 自动提取参数） ────────────────────────
        PREFIX = {
            "/new":     (4, self._cmd_new),
            "/rename":  (7, self._cmd_rename),
            "/switch":  (7, self._cmd_switch),
            "/del":     (4, self._cmd_del),
            "/view":    (5, self._cmd_view),
            "/history": (8, None),  # None = 特殊处理
        }
        for prefix, (plen, handler) in PREFIX.items():
            if stripped.startswith(prefix):
                arg = stripped[plen:].strip()
                if handler is None:  # /history 特殊：解析数字参数
                    n = config.HISTORY_DISPLAY_N
                    if arg:
                        try:
                            n = int(arg)
                            n = min(max(n, 1), config.HISTORY_DISPLAY_MAX)
                        except ValueError:
                            await self.feishu.reply_message(
                                msg_id,
                                FeishuClient.build_card("❌ 参数必须是数字，如 `/history 10`")
                            )
                            return True
                    await self._cmd_history(msg_id, n)
                    return True
                # /switch 和 /del 需要参数
                if prefix in ("/switch", "/del") and not arg:
                    await self.feishu.reply_message(
                        msg_id,
                        FeishuClient.build_card(f"❌ 请指定序号，如 `{prefix} 1`\n\n发送 `/list` 查看所有对话")
                    )
                    return True
                # /new 允许无参数（默认"新对话"）
                if prefix == "/new" and not arg:
                    arg = "新对话"
                await handler(msg_id, arg)
                return True

        return False

    async def _cmd_start(self, msg_id: str) -> None:
        """/start 命令：已在活跃状态时提示。"""
        await self.feishu.reply_message(
            msg_id,
            FeishuClient.build_card("🟢 Bot 已在活跃状态，直接发消息即可。")
        )

    async def _cmd_view(self, msg_id: str, file_path: str) -> None:
        """/view <路径> — 查看 workspace 下的文件，上传到飞书发送。"""
        if not file_path:
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card("❌ 请指定文件路径，如 `/view workspace/output_0601-143000.md`")
            )
            return
        from pathlib import Path
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(config.WORKSPACE_DIR) / file_path
        if not path.exists():
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card(f"❌ 文件不存在: `{file_path}`")
            )
            return
        # 上传并发送
        file_key = await self.feishu.upload_file(str(path))
        if not file_key:
            await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card(f"❌ 文件上传失败: `{path.name}`")
            )
            return
        await self.feishu.reply_file(msg_id, file_key)
        await self.feishu.reply_message(
            msg_id,
            FeishuClient.build_card(f"📄 `{path.name}` — 点击上方文件预览")
        )

    # ── 消息处理 ────────────────────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        """处理单条飞书消息。"""
        msg_id = msg.get("message_id", "")
        if not msg_id:
            return

        # 去重：用 create_time 判断
        msg_create_time = msg.get("create_time", "0")
        try:
            msg_ts = int(msg_create_time)
        except (ValueError, TypeError):
            msg_ts = 0

        last_ts = state_store.state.last_message_ts or 0
        if msg_ts < last_ts:
            return
        if msg_ts == last_ts and msg_id == state_store.state.last_message_id:
            return

        state_store.update(last_message_ts=msg_ts, last_message_id=msg_id)

        # 提取消息内容
        content_str = msg.get("body", {}).get("content", "{}")
        if isinstance(content_str, bytes):
            content_str = content_str.decode("utf-8", errors="replace")
        text = FeishuClient.extract_text(content_str)

        sender = msg.get("sender", {})
        sender_id = sender.get("sender_id", {})
        sender_open_id = sender_id.get("open_id", "") if isinstance(sender_id, dict) else ""

        chat_id = msg.get("chat_id", "")
        message_type = msg.get("msg_type", "text")

        if message_type != "text":
            return
        if sender_open_id and sender_open_id != config.FEISHU_OPEN_ID:
            return
        if not text:
            return

        if chat_id and not state_store.state.chat_id:
            state_store.update(chat_id=chat_id)
            log.info(f"chat_id discovered: {chat_id}")

        log.info(f"handling message: {text[:80]!r}")

        # 更新最后活跃时间
        self._last_active = time.monotonic()

        # ── 休眠模式：仅响应 /start ─────────────────────────────────
        stripped = text.strip()

        if self._idle:
            if stripped == "/start":
                self._idle = False
                self._last_active = time.monotonic()
                log.info("ACTIVATED by /start")
                await self.feishu.reply_message(
                    msg_id,
                    FeishuClient.build_card(
                        "🟢 **Bot 已激活**\n\n"
                        "开始对话吧！发送 `/help` 查看所有命令。\n"
                        "发送 `/stop` 休眠。"
                    )
                )
                return
            elif stripped == "/status":
                await self._cmd_status(msg_id)
                return
            elif stripped == "/help" or stripped == "/h":
                await self._cmd_help(msg_id)
                return
            else:
                # 休眠中非 /start 消息：提示唤醒
                await self.feishu.reply_message(
                    msg_id,
                    FeishuClient.build_card("😴 **Bot 休眠中**\n\n发送 **`/start`** 唤醒我。")
                )
                return

        # ── 命令路由 ─────────────────────────────────────────────────
        if await self._route_command(stripped, msg_id):
            return

        # ── 飞书文档检测 ──────────────────────────────────────────────
        import re
        doc_urls = re.findall(r'https?://[^\s]*feishu\.cn/(?:docx|wiki)/[^\s]+', text)
        doc_context = ""
        if doc_urls:
            for url in doc_urls:
                log.info("detected feishu doc url: %s", url[:80])
                doc = await self.feishu.fetch_document(url)
                if doc:
                    doc_context += f"\n\n---\n📄 文档《{doc['title']}》内容:\n{doc['content'][:8000]}\n---\n"
                    # 通知用户已读取
                    await self.feishu.reply_message(
                        msg_id,
                        FeishuClient.build_card(f"📄 已读取文档，正在分析...")
                    )
            if doc_context:
                text = text + doc_context

        # ── 执行 Claude ──────────────────────────────────────────────

        async with self._run_lock:
            active = conv_store.ensure_default()
            state_store.update(active_conv_id=active.id)

            # 保存用户消息到历史
            history_store.append(active.id, "user", text)

            # 关键改进：如果当前对话的 Claude 进程正在运行，新消息通过 queue 写入 stdin
            if self._is_claude_running_for_conv(active.id):
                log.info(f"queuing message to running Claude: {text[:50]!r}")
                await self._message_queue.put({
                    "msg_type": "user_message",
                    "text": text,
                    "user_message_id": msg_id,
                })
                return

            # Claude 不在运行，启动新的
            await self.stop_claude()  # 清理可能残留的旧进程

            # 创建消息队列
            q: asyncio.Queue = asyncio.Queue()
            self._message_queue = q
            self._conv_queue[active.id] = q

            # 回复"正在思考"
            conv_hint = f"💬 **{active.title}**\n\n⏳ 正在思考..."
            reply_msg_id = await self.feishu.reply_message(
                msg_id,
                FeishuClient.build_card(conv_hint)
            )
            if not reply_msg_id:
                log.error("failed to send thinking message")
                return

            # 启动 Claude 流式执行
            self._claude_task = asyncio.create_task(
                run_claude_and_stream(
                    feishu=self.feishu,
                    text=text,
                    thinking_msg_id=reply_msg_id,
                    user_message_id=msg_id,
                    session_id=active.session_id,
                    workspace=active.workspace,
                    on_proc=self._register_proc,
                    message_queue=q,
                    on_stdin_close=self._on_stdin_close,
                )
            )

    # ── 轮询主循环 ──────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """轮询飞书消息。休眠态 30s 间隔，活跃态 2s 间隔。活跃态 30min 无消息自动休眠。"""
        IDLE_INTERVAL = config.IDLE_POLL_INTERVAL  # 从配置读取
        log.info("poll loop started (idle mode)")
        err_count = 0
        while True:
            # 动态间隔：休眠 30s，活跃 2s
            base_interval = IDLE_INTERVAL if self._idle else config.POLL_INTERVAL
            sleep_time = base_interval

            # 活跃态自动休眠检测
            if not self._idle:
                idle_sec = time.monotonic() - self._last_active
                if idle_sec > self._auto_idle_sec:
                    log.info(f"auto-idle after {idle_sec:.0f}s inactive")
                    if self._is_claude_running():
                        await self.stop_claude()
                    self._idle = True
                    continue

            try:
                chat_id = state_store.state.chat_id
                if not chat_id:
                    await asyncio.sleep(base_interval)
                    continue

                messages = await self.feishu.get_messages(chat_id)
                if messages is None:  # API 错误 → 指数退避
                    err_count += 1
                    sleep_time = min(base_interval * (2 ** min(err_count, 5)), 60.0)
                    log.error(f"get_messages API error, backing off to {sleep_time}s (err_count={err_count})")
                else:
                    err_count = 0  # 成功，重置计数
                    if messages:
                        # 过滤新消息：用 create_time 判断
                        last_ts = state_store.state.last_message_ts or 0
                        new_msgs = []
                        for m in messages:
                            ct = m.get("create_time", "0")
                            try:
                                ct_int = int(ct)
                            except (ValueError, TypeError):
                                ct_int = 0
                            if ct_int > last_ts:
                                new_msgs.append(m)
                            elif ct_int == last_ts and m.get("message_id", "") != state_store.state.last_message_id:
                                new_msgs.append(m)

                        if new_msgs:
                            new_msgs.sort(key=lambda m: m.get("create_time", "0"))
                            for msg in new_msgs:
                                await self._handle_message(msg)

            except Exception as e:
                log.error(f"poll loop error: {e}")
                err_count += 1
                sleep_time = min(base_interval * (2 ** min(err_count, 5)), 60.0)

            await asyncio.sleep(sleep_time)

    # ── 启动与关闭 ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 Bot。"""
        from config import setup_logging
        setup_logging()

        Path(config.WORKSPACE_DIR).mkdir(parents=True, exist_ok=True)
        Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)

        state_store.load()
        log.info("state loaded: chat_id=%s, active_conv_id=%s",
                 state_store.state.chat_id, state_store.state.active_conv_id)

        conv_store.load(active_conv_id=state_store.state.active_conv_id)
        conv_store.ensure_default()
        log.info("conversations loaded: %d convs, active=%s",
                 len(conv_store.list_all()),
                 conv_store.active.id if conv_store.active else None)

        if conv_store.active:
            state_store.update(active_conv_id=conv_store.active.id)

        if not state_store.state.chat_id:
            await self._discover_chat_id()

        try:
            await self._poll_loop()
        finally:
            await self.feishu.close()

    async def shutdown(self) -> None:
        """关闭 Bot。"""
        log.info("shutting down...")
        await self.stop_claude()
        await self.feishu.close()


async def main():
    bot = Bot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

