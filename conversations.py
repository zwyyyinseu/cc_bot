"""
多对话管理 —— Conversation 数据模型 + 持久化存储。

每个对话对应一个独立的 Claude 会话（session_id）和工作目录，
用户可以在飞书上创建、切换、删除多个对话。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import config
import logging
log = logging.getLogger(__name__)


def _uid() -> str:
    """生成 8 位短 ID，方便手机输入。"""
    return uuid.uuid4().hex[:8]


def _now_iso() -> str:
    """当前 UTC 时间 ISO 格式。"""
    return datetime.now(timezone.utc).isoformat()


def _time_ago(iso_str: str) -> str:
    """将 ISO 时间字符串转为人类可读的相对时间。"""
    try:
        dt = datetime.fromisoformat(iso_str)
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return f"{seconds // 60}分钟前"
        if seconds < 86400:
            return f"{seconds // 3600}小时前"
        return f"{seconds // 86400}天前"
    except Exception:
        return iso_str


@dataclass
class Conversation:
    id: str = field(default_factory=_uid)
    title: str = "新对话"
    session_id: Optional[str] = None       # Claude --resume 用的 session_id
    workspace: str = ""                     # Claude 工作目录
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


class ConversationStore:
    def __init__(self) -> None:
        self._path = Path(config.DATA_DIR) / "conversations.json"
        self._convs: list[Conversation] = []
        self._active_id: Optional[str] = None  # 当前活跃对话 ID

    def load(self, active_conv_id: Optional[str] = None) -> None:
        """从 JSON 文件加载对话列表。"""
        self._active_id = active_conv_id
        if not self._path.exists():
            self._convs = []
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._convs = [
                Conversation(**{k: v for k, v in item.items()
                               if k in Conversation.__dataclass_fields__})
                for item in raw
            ]
        except Exception as e:
            log.error(f"load failed: {e}")
            self._convs = []

    def save(self) -> None:
        """持久化对话列表到 JSON 文件。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps([asdict(c) for c in self._convs], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.rename(self._path)
        except Exception as e:
            log.error(f"save failed: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    # ── 查询 ────────────────────────────────────────────────────────────

    def list_all(self) -> list[Conversation]:
        """返回所有对话，按 updated_at 倒序（最近活跃的在前）。"""
        return sorted(self._convs, key=lambda c: c.updated_at, reverse=True)

    def get(self, conv_id: str) -> Optional[Conversation]:
        """按 ID 获取对话。"""
        for c in self._convs:
            if c.id == conv_id:
                return c
        return None

    def get_by_index(self, index: int) -> Optional[Conversation]:
        """按显示序号获取对话（1-based，按 list_all 的顺序）。"""
        ordered = self.list_all()
        if 1 <= index <= len(ordered):
            return ordered[index - 1]
        return None

    @property
    def active(self) -> Optional[Conversation]:
        """返回当前活跃对话。"""
        if self._active_id:
            return self.get(self._active_id)
        return None

    @property
    def active_id(self) -> Optional[str]:
        return self._active_id

    # ── 修改 ────────────────────────────────────────────────────────────

    def create(self, title: str = "新对话", workspace: Optional[str] = None) -> Conversation:
        """创建新对话并设为活跃。"""
        conv = Conversation(
            title=title,
            workspace=workspace or config.WORKSPACE_DIR,
        )
        self._convs.append(conv)
        self._active_id = conv.id
        self.save()
        return conv

    def update(self, conv_id: str, **kwargs) -> None:
        """更新对话字段。"""
        conv = self.get(conv_id)
        if not conv:
            return
        for k, v in kwargs.items():
            if hasattr(conv, k):
                setattr(conv, k, v)
        conv.updated_at = _now_iso()
        self.save()

    def delete(self, conv_id: str) -> bool:
        """删除对话，返回是否成功。"""
        conv = self.get(conv_id)
        if not conv:
            return False
        self._convs.remove(conv)
        # 如果删除的是活跃对话，切换到最近的对话
        if self._active_id == conv_id:
            ordered = self.list_all()
            self._active_id = ordered[0].id if ordered else None
        self.save()
        return True

    def set_active(self, conv_id: str) -> bool:
        """设置活跃对话，返回是否成功。"""
        conv = self.get(conv_id)
        if not conv:
            return False
        self._active_id = conv_id
        conv.updated_at = _now_iso()
        self.save()
        return True

    def ensure_default(self) -> Conversation:
        """确保至少有一个对话（无对话时自动创建默认对话）。"""
        if self._convs:
            if not self._active_id:
                ordered = self.list_all()
                self._active_id = ordered[0].id
                self.save()
            return self.active or self._convs[0]
        return self.create(title="默认对话")

    # ── 格式化输出 ──────────────────────────────────────────────────────

    def format_list(self) -> str:
        """格式化对话列表为飞书卡片 Markdown。"""
        ordered = self.list_all()
        if not ordered:
            return "📭 暂无对话\n\n💡 发送 `/new 标题` 创建新对话"

        lines = [f"📑 **对话列表**（共 {len(ordered)} 个）\n"]
        for i, conv in enumerate(ordered, 1):
            active_marker = " ▸ " if conv.id == self._active_id else "   "
            active_tag = " **[活跃]**" if conv.id == self._active_id else ""
            time_ago = _time_ago(conv.updated_at)
            sid_status = "✓" if conv.session_id else "○"
            lines.append(f"{active_marker}{i}. **{conv.title}**{active_tag}  {sid_status} {time_ago}")

        lines.append("\n💡 `/switch 序号` 切换  `/del 序号` 删除  `/new 标题` 新建")
        return "\n".join(lines)


# 单例
conv_store = ConversationStore()

