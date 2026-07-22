"""
花费统计 —— 记录每次 Claude API 调用的费用，支持按天/月/总计查询。

格式（每行一条记录）：
  {"conv_id": "425ffdf7", "cost_usd": 0.0423, "timestamp": "2026-07-22T15:30:00+00:00"}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from config import config
import logging

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CostTracker:
    def __init__(self) -> None:
        self._path = Path(config.DATA_DIR) / "costs.jsonl"

    # ── 写入 ──────────────────────────────────────────────────────────────

    def record(self, conv_id: str, cost_usd: float) -> None:
        """记录一次花费。cost_usd 为 0 时跳过。"""
        if cost_usd <= 0:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "conv_id": conv_id,
            "cost_usd": cost_usd,
            "timestamp": _now_iso(),
        }
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"cost record failed: {e}")

    # ── 查询 ──────────────────────────────────────────────────────────────

    def _load_all(self) -> list[dict]:
        """加载全部记录。"""
        if not self._path.exists():
            return []
        entries: list[dict] = []
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            log.error(f"cost load failed: {e}")
        return entries

    def query(self) -> dict:
        """查询今日、本月、总计花费。"""
        now = datetime.now(timezone.utc)
        today_key = now.strftime("%Y-%m-%d")
        month_key = now.strftime("%Y-%m")

        today_total = 0.0
        month_total = 0.0
        all_total = 0.0

        for entry in self._load_all():
            cost = entry.get("cost_usd", 0)
            ts = entry.get("timestamp", "")
            all_total += cost

            # 按月份（前 7 个字符匹配 YYYY-MM）
            if ts[:7] == month_key:
                month_total += cost
                # 按天（前 10 个字符匹配 YYYY-MM-DD）
                if ts[:10] == today_key:
                    today_total += cost

        return {
            "today": today_total,
            "month": month_total,
            "total": all_total,
        }

    def count(self) -> int:
        """返回总记录条数。"""
        if not self._path.exists():
            return 0
        try:
            return sum(1 for line in self._path.read_text(encoding="utf-8").splitlines() if line.strip())
        except Exception:
            return 0


# 单例
cost_tracker = CostTracker()
