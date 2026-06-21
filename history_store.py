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
        except Exception as e:
            print(f"[history_store] append failed conv={conv_id}: {e}")

    def get_recent(self, conv_id: str, n: int = 10) -> list[dict]:
        """取最近 N 条记录（按时间正序），n 上限 20。
        若本地无历史文件，自动尝试从 Claude session 导入。
        """
        n = min(max(n, 1), 20)
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
            print(f"[history_store] read failed conv={conv_id}: {e}")
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
            print(f"[history_store] delete failed conv={conv_id}: {e}")

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
                    if msg_type == "user":
                        # 提取用户消息文本
                        content = obj.get("message", {}).get("content", [])
                        texts = []
                        for c in content:
                            if c.get("type") == "text":
                                t = c.get("text", "").strip()
                                if t:
                                    texts.append(t)
                        full_text = " ".join(texts)
                        if full_text:
                            ts = obj.get("timestamp", _now_iso())
                            entries.append({"role": "user", "text": full_text, "timestamp": ts})

                    elif msg_type == "assistant":
                        # 跳过子代理的消息（有 parentUuid 关联到子代理 session）
                        # 只取顶层 assistant 消息中的 text 块
                        content = obj.get("message", {}).get("content", [])
                        if not isinstance(content, list):
                            continue
                        texts = []
                        for c in content:
                            if c.get("type") == "text":
                                t = c.get("text", "").strip()
                                # 跳过网络超时等错误占位
                                if t and t not in ("Request timed out",):
                                    texts.append(t)
                        full_text = " ".join(texts)
                        if full_text:
                            ts = obj.get("timestamp", _now_iso())
                            entries.append({"role": "assistant", "text": full_text, "timestamp": ts})

        except Exception as e:
            print(f"[history_store] import failed conv={conv_id}: {e}")
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
            print(f"[history_store] write after import failed conv={conv_id}: {e}")
            return 0

        print(f"[history_store] imported {len(entries)} entries from {session_path.name} → conv={conv_id}")
        return len(entries)


# 单例
history_store = HistoryStore()
