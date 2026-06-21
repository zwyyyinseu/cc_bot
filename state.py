"""
持久化状态管理 —— JSON 文件存储全局状态（chat_id、去重标记、活跃对话 ID）。
对话相关的状态（session_id、workspace）由 conversations.py 管理。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from config import config


@dataclass
class State:
    last_message_id: str = ""              # 最后处理的消息 ID（同一秒内去重用）
    last_message_ts: int = 0               # 最后处理的消息时间戳（主要去重依据）
    chat_id: str | None = None             # 飞书会话 ID（首次消息时自动获取）
    active_conv_id: str | None = None      # 当前活跃对话 ID


class StateStore:
    def __init__(self) -> None:
        self._path = Path(config.DATA_DIR) / "state.json"
        self._state = State()

    def load(self) -> State:
        """从 JSON 文件加载状态，文件不存在或损坏时返回默认值。"""
        if not self._path.exists():
            return self._state
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._state = State(**{k: v for k, v in raw.items() if k in State.__dataclass_fields__})
        except Exception as e:
            print(f"[state] load failed, using defaults: {e}")
            self._state = State()
        return self._state

    def save(self) -> None:
        """原子写入状态到 JSON 文件。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(asdict(self._state), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.rename(self._path)
        except Exception as e:
            print(f"[state] save failed: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    @property
    def state(self) -> State:
        return self._state

    def update(self, **kwargs) -> None:
        """局部更新状态字段并持久化。"""
        for k, v in kwargs.items():
            if hasattr(self._state, k):
                setattr(self._state, k, v)
        self.save()


# 单例
state_store = StateStore()
