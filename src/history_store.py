"""
对话历史持久化 —— 每个对话一个 JSONL 文件，追加写入。
支持从 Claude 原生 session JSONL 文件导入历史记录。

格式（每行一个 JSON 对象）：
  {"role": "user", "text": "...", "timestamp": "2026-06-21T13:30:00+00:00"}
  {"role": "assistant", "text": "...", "timestamp": "2026-06-21T13:32:00+00:00"}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import config
import logging
log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claude_session_path(workspace: str, session_id: str) -> Optional[Path]:
    """根据 workspace 和 session_id 定位 Claude 原生 session JSONL 文件。

    优先按 workspace 精确匹配；若不匹配（目录改名等），全局搜索 session_id 文件名。
    """
    if not workspace or not session_id:
        return None
    sanitized = workspace.replace("/", "-")
    exact = Path.home() / ".claude" / "projects" / sanitized / f"{session_id}.jsonl"
    if exact.exists():
        return exact
    # 目录名可能不匹配（重命名等），全局搜索
    projects_dir = Path.home() / ".claude" / "projects"
    if projects_dir.exists():
        for f in projects_dir.glob(f"*/{session_id}.jsonl"):
            return f
    return None


class HistoryStore:
    def __init__(self) -> None:
        self._base = Path(config.DATA_DIR) / "history"

    def _path(self, conv_id: str) -> Path:
        return self._base / f"{conv_id}.jsonl"

    def append(self, conv_id: str, role: str, text: str) -> None:
        """追加一条历史记录。"""
        if not text.strip():
            return
        path = self._path(conv_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "role": role,
            "text": text,
            "timestamp": _now_iso(),
        }
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # 超过行数上限或文件大小上限时截断
            lines = path.read_text(encoding="utf-8").splitlines()
            file_size = path.stat().st_size
            need_truncate = (
                len(lines) > config.HISTORY_FILE_MAX_LINES * 2
                or file_size > config.HISTORY_FILE_MAX_BYTES * 2
            )
            if need_truncate:
                # 从后往前取，保留最近的内容
                keep_lines = lines[-config.HISTORY_FILE_MAX_LINES:]
                path.write_text(
                    "\n".join(keep_lines) + "\n",
                    encoding="utf-8"
                )
                log.info("history truncated conv=%s: %d→%d lines, %d→%d bytes",
                         conv_id, len(lines), len(keep_lines),
                         file_size, path.stat().st_size)
        except Exception as e:
            log.error(f"append failed conv={conv_id}: {e}")

    def get_recent(self, conv_id: str, n: int = None) -> list[dict]:
        """取最近 N 条记录（按时间正序）。
        若本地无历史文件，自动尝试从 Claude session 导入。
        """
        if n is None:
            n = config.HISTORY_DISPLAY_N
        n = min(max(n, 1), config.HISTORY_DISPLAY_MAX)
        path = self._path(conv_id)
        if not path.exists():
            return []
        entries: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # 跳过损坏的行
        except Exception as e:
            log.error(f"read failed conv={conv_id}: {e}")
            return []
        # 返回最后 n 条（按时间正序）
        return entries[-n:]

    def count(self, conv_id: str) -> int:
        """返回总记录条数。"""
        path = self._path(conv_id)
        if not path.exists():
            return 0
        try:
            # 只计非空行
            text = path.read_text(encoding="utf-8")
            return sum(1 for line in text.splitlines() if line.strip())
        except Exception:
            return 0

    def delete(self, conv_id: str) -> None:
        """删除对话的全部历史文件。"""
        path = self._path(conv_id)
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            log.error(f"delete failed conv={conv_id}: {e}")

    # ── Claude Session 导入 ─────────────────────────────────────────────

    def import_from_claude(self, conv_id: str, workspace: str, session_id: str) -> int:
        """从 Claude 原生 session JSONL 文件导入历史到本地。
        只在本地无历史文件时执行，避免重复导入。
        返回导入条数，0 表示无数据或已存在本地历史。
        """
        # 已有本地历史则跳过
        local_path = self._path(conv_id)
        if local_path.exists():
            return 0

        session_path = _claude_session_path(workspace, session_id)
        if not session_path:
            return 0

        entries: list[dict] = []
        try:
            with open(session_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = obj.get("type", "")
                    # 跳过子代理的消息
                    if obj.get("parentUuid") or obj.get("isSidechain"):
                        continue
                    message = obj.get("message", {})
                    # message 可能是字符串（某些旧版/异常格式），跳过
                    if not isinstance(message, dict):
                        continue
                    content = message.get("content", [])

                    # content 可能是字符串（直接文本）或列表（content blocks）
                    if isinstance(content, str):
                        texts = [content.strip()]
                    elif isinstance(content, list):
                        texts = []
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                t = c.get("text", "").strip()
                                if t:
                                    texts.append(t)
                    else:
                        continue

                    full_text = " ".join(texts)
                    if not full_text:
                        continue

                    if msg_type == "user":
                        ts = obj.get("timestamp", _now_iso())
                        entries.append({"role": "user", "text": full_text, "timestamp": ts})

                    elif msg_type == "assistant":
                        # 跳过网络超时等错误占位
                        if full_text in ("Request timed out",):
                            continue
                        ts = obj.get("timestamp", _now_iso())
                        entries.append({"role": "assistant", "text": full_text, "timestamp": ts})

        except Exception as e:
            log.error(f"import failed conv={conv_id}: {e}")
            return 0

        if not entries:
            return 0

        # 批量写入本地文件
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(local_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"write after import failed conv={conv_id}: {e}")
            return 0

        log.info(f"imported {len(entries)} entries from {session_path.name} → conv={conv_id}")
        return len(entries)


# 单例
history_store = HistoryStore()

