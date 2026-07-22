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

    # ── 历史导入 ──────────────────────────────────────────────────────────

    def import_history(self) -> int:
        """从 Claude session JSONL 文件中导入历史花费记录。
        扫描 ~/.claude/projects/ 下所有 session 文件，
        提取 result 事件中的 cost_usd。
        只在 costs.jsonl 不存在或为空时执行。
        返回导入的记录数。
        """
        # 已有记录则跳过
        if self._path.exists() and self._path.stat().st_size > 0:
            return 0

        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.exists():
            return 0

        imported = 0
        for jsonl in projects_dir.glob("*/*.jsonl"):
            try:
                for line in jsonl.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "result":
                        cost = obj.get("cost_usd", 0)
                        if cost and cost > 0:
                            ts = obj.get("timestamp", _now_iso())
                            self._write_entry({
                                "conv_id": jsonl.stem,
                                "cost_usd": cost,
                                "timestamp": ts,
                            })
                            imported += 1
            except Exception as e:
                log.warning(f"import skipped {jsonl.name}: {e}")

        if imported:
            log.info(f"cost import: {imported} records from session files")
        return imported

    def _write_entry(self, entry: dict) -> None:
        """内部写入方法，绕过 record 的重复检查。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"cost write failed: {e}")


# 单例
cost_tracker = CostTracker()
