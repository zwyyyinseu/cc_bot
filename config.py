"""
配置管理 —— 从 .env 文件和环境变量读取配置。
不依赖 python-dotenv，手动解析 key=value 格式。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent

# ── 日志 ──────────────────────────────────────────────────────────────────

def setup_logging(level: int = logging.INFO) -> None:
    """配置全局日志：输出到 stderr（start.sh 将 stderr 重定向到 bot.log）。"""
    fmt = logging.Formatter(
        "[%(name)s] %(message)s", datefmt="%m-%d %H:%M:%S"
    )
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(sh)
    # 抑制第三方库冗余日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _load_dotenv(path: Path) -> dict[str, str]:
    """手动解析 .env 文件，返回 key→value 映射。"""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


_dotenv = _load_dotenv(_BASE_DIR / ".env")


def _get(key: str, default: str = "") -> str:
    """优先取环境变量，其次取 .env 文件。"""
    return os.environ.get(key, _dotenv.get(key, default))


@dataclass
class Config:
    # 飞书凭证
    FEISHU_APP_ID: str = field(default_factory=lambda: _get("FEISHU_APP_ID"))
    FEISHU_APP_SECRET: str = field(default_factory=lambda: _get("FEISHU_APP_SECRET"))
    FEISHU_OPEN_ID: str = field(default_factory=lambda: _get("FEISHU_OPEN_ID"))

    # 轮询和节流间隔
    POLL_INTERVAL: float = 2.0       # 消息轮询间隔（秒，活跃态）
    THROTTLE_INTERVAL: float = 5.0   # 卡片更新节流间隔（秒）

    # Claude 配置
    CLAUDE_BIN: str = field(default_factory=lambda: _get("CLAUDE_BIN", "claude"))

    # 目录
    WORKSPACE_DIR: str = str(_BASE_DIR / "workspace")
    DATA_DIR: str = str(_BASE_DIR / "data")

    # 卡片内容上限（字符数）
    CARD_MAX_CHARS: int = 4000

    # ── 休眠/唤醒 ──────────────────────────────────────────────────────
    IDLE_POLL_INTERVAL: float = 30.0      # 休眠态轮询间隔（秒）
    AUTO_IDLE_SEC: float = 1800.0         # 活跃态无消息自动休眠（秒）

    # ── Claude 超时 ─────────────────────────────────────────────────────
    RESULT_TIMEOUT: float = 600.0         # 单轮最长等待（秒）

    # ── 历史记录 ────────────────────────────────────────────────────────
    HISTORY_MAX_CHARS: int = 250          # /history 单条消息截断字符数
    HISTORY_DISPLAY_N: int = 10           # /history 默认显示条数
    HISTORY_DISPLAY_MAX: int = 20         # /history 最大显示条数
    HISTORY_FILE_MAX_LINES: int = 500     # 历史文件最大行数，超出时截断


config = Config()
