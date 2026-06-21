"""
对话历史持久化 —— 每个对话一个 JSONL 文件，追加写入。

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
        """取最近 N 条记录（按时间正序），n 上限 20。"""
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


# 单例
history_store = HistoryStore()
