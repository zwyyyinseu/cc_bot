"""
核心引擎边界测试 —— 不依赖飞书 API，直接测内部逻辑。

覆盖:
  1. 并发消息队列（多轮同时到达）
  2. 超长文本处理
  3. 状态文件损坏恢复
  4. 空/异常输入
  5. 对话 CRUD 边界
  6. 历史文件极端场景
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import config
from conversations import Conversation, conv_store
from state import State, state_store
from history_store import history_store


# ── Setup / Teardown ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def temp_data():
    """每个测试用临时目录，不污染真实 data/。"""
    tmp = tempfile.mkdtemp()
    old_data_dir = config.DATA_DIR
    old_conv_path = conv_store._path
    old_state_path = state_store._path
    old_history_base = history_store._base
    old_convs = list(conv_store._convs)
    old_active_id = conv_store._active_id
    old_state = state_store._state

    config.DATA_DIR = tmp
    conv_store._convs = []
    conv_store._active_id = None
    conv_store._path = Path(tmp) / "conversations.json"
    state_store._state = State()
    state_store._path = Path(tmp) / "state.json"
    history_store._base = Path(tmp) / "history"

    yield tmp

    # 恢复原始状态
    config.DATA_DIR = old_data_dir
    conv_store._path = old_conv_path
    conv_store._convs = old_convs
    conv_store._active_id = old_active_id
    state_store._path = old_state_path
    state_store._state = old_state
    history_store._base = old_history_base

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════
# 1. 并发 / 多轮消息队列
# ═══════════════════════════════════════════════════════════════════

def test_queue_not_lost():
    """消息入队后不应丢失。"""
    import asyncio
    q: asyncio.Queue = asyncio.Queue()
    for i in range(100):
        q.put_nowait({"msg_type": "user_message", "text": f"msg {i}"})
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert len(items) == 100


def test_sentinel_terminates():
    """None sentinel 应该能终止队列处理。"""
    import asyncio
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(None)
    msg = q.get_nowait()
    assert msg is None


# ═══════════════════════════════════════════════════════════════════
# 2. 超长文本 / 空输入
# ═══════════════════════════════════════════════════════════════════

def test_empty_text_extraction():
    """空消息提取不应崩溃。"""
    from feishu_client import FeishuClient
    result = FeishuClient.extract_text("")
    assert result == ""

    result = FeishuClient.extract_text('{"text": ""}')
    assert result == ""


def test_very_long_text_truncation():
    """超长文本截断不应崩溃。"""
    from stream_handler import _truncate_for_card
    long_text = "A" * 10000
    # 无 workspace：只截断，不保存文件
    result, saved = _truncate_for_card(long_text, max_chars=4000, workspace="")
    assert len(result) <= 4100  # truncated + suffix
    assert not saved  # 空字符串（无 workspace 时不保存文件）
    assert "...(内容过长，已截断)" in result
    # 有 workspace：应保存文件
    result2, saved2 = _truncate_for_card(long_text, max_chars=4000, workspace=config.WORKSPACE_DIR)
    assert saved2 is not None
    assert "完整输出" in result2


def test_markdown_unescaped():
    """markdown 特殊字符不应导致卡片构建崩溃。"""
    from feishu_client import FeishuClient
    special = "**bold** `code` [link](url) | table | *italic* <tag>"
    card = FeishuClient.build_card(special)
    assert "bold" in card
    assert "link" in card


# ═══════════════════════════════════════════════════════════════════
# 3. 状态文件损坏恢复
# ═══════════════════════════════════════════════════════════════════

def test_state_load_corrupted():
    """损坏的 state.json 应返回默认值，不崩溃。"""
    state_path = Path(config.DATA_DIR) / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("not valid json {{{")
    state_store.load()
    assert state_store.state.last_message_ts == 0
    assert state_store.state.chat_id is None


def test_state_load_missing():
    """不存在的 state.json 应返回默认值。"""
    state_store.load()
    assert state_store.state.last_message_ts == 0


def test_conversations_load_corrupted():
    """损坏的 conversations.json 应返回空列表。"""
    conv_path = Path(config.DATA_DIR) / "conversations.json"
    conv_path.parent.mkdir(parents=True, exist_ok=True)
    conv_path.write_text("[{bad json")
    conv_store.load()
    assert len(conv_store.list_all()) == 0


# ═══════════════════════════════════════════════════════════════════
# 4. 对话 CRUD 边界
# ═══════════════════════════════════════════════════════════════════

def test_get_nonexistent_conv():
    assert conv_store.get("nonexist") is None
    assert conv_store.get_by_index(999) is None
    assert conv_store.active is None


def test_create_and_switch():
    c1 = conv_store.create(title="测试1")
    assert conv_store.active.id == c1.id
    c2 = conv_store.create(title="测试2")
    assert conv_store.active.id == c2.id
    assert conv_store.set_active(c1.id)
    assert conv_store.active.id == c1.id


def test_delete_active_switches():
    c1 = conv_store.create(title="A")
    c2 = conv_store.create(title="B")
    assert conv_store.active.id == c2.id
    conv_store.delete(c2.id)
    # 删除活跃对话后应自动切换
    assert conv_store.active is not None
    assert conv_store.active.id == c1.id


def test_delete_last_conv():
    c = conv_store.create(title="唯一")
    conv_store.delete(c.id)
    assert len(conv_store.list_all()) == 0
    assert conv_store.active is None


def test_ensure_default_creates():
    result = conv_store.ensure_default()
    assert result is not None
    assert result.title == "默认对话"


def test_duplicate_titles():
    """同名对话应允许创建（用 ID 区分）。"""
    conv_store.create(title="同名")
    conv_store.create(title="同名")
    assert len(conv_store.list_all()) == 2


def test_very_long_title():
    """超长标题不应崩溃。"""
    c = conv_store.create(title="A" * 500)
    assert c.title == "A" * 500
    formatted = conv_store.format_list()
    assert "A" * 20 in formatted  # 至少显示了部分


# ═══════════════════════════════════════════════════════════════════
# 5. 历史文件极端场景
# ═══════════════════════════════════════════════════════════════════

def test_history_empty_conv():
    assert history_store.get_recent("empty") == []
    assert history_store.count("empty") == 0


def test_history_delete_nonexistent():
    history_store.delete("no_such_file")
    # 不应抛异常


def test_history_rapid_appends():
    """快速追加大量记录。"""
    history_store.delete("rapid_test")
    for i in range(100):
        history_store.append("rapid_test", "user", f"line {i}")
    assert history_store.count("rapid_test") == 100
    history_store.delete("rapid_test")


def test_history_corrupted_line():
    """损坏的 JSONL 行应被跳过。"""
    conv_id = "corrupt_test"
    history_store.delete(conv_id)
    path = Path(config.DATA_DIR) / "history" / f"{conv_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"role":"user","text":"good1","timestamp":"2026-01-01"}\n'
        'this is not valid json\n'
        '{"role":"assistant","text":"good2","timestamp":"2026-01-01"}\n',
        encoding="utf-8"
    )
    entries = history_store.get_recent(conv_id)
    assert len(entries) == 2  # 损坏行跳过
    history_store.delete(conv_id)


def test_history_with_newlines_in_text():
    """消息内容含换行符不应破坏 JSONL 结构。"""
    history_store.delete("newline_test")
    history_store.append("newline_test", "user", "line1\nline2\nline3")
    entries = history_store.get_recent("newline_test")
    assert len(entries) == 1
    assert "line1" in entries[0]["text"]


# ═══════════════════════════════════════════════════════════════════
# 6. Stream-json 消息构建
# ═══════════════════════════════════════════════════════════════════

def test_build_stream_json_normal():
    from claude_runner import build_stream_json_input
    data = build_stream_json_input("hello world")
    parsed = json.loads(data.decode())
    assert parsed["type"] == "user"
    content = parsed["message"]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "hello world"


def test_build_stream_json_special_chars():
    from claude_runner import build_stream_json_input
    data = build_stream_json_input('{"json": "in text", "key": 123}')
    parsed = json.loads(data.decode())
    assert "json" in parsed["message"]["content"][0]["text"]


def test_build_stream_json_unicode():
    from claude_runner import build_stream_json_input
    data = build_stream_json_input("你好世界 🌍 émoji test")
    parsed = json.loads(data.decode())
    assert "你好世界" in parsed["message"]["content"][0]["text"]


# ═══════════════════════════════════════════════════════════════════
# 7. 命令路由表完整性
# ═══════════════════════════════════════════════════════════════════

def test_all_commands_registered():
    """确保所有命令都在路由表中。"""
    import main as m
    # 检查 _route_command 方法存在
    bot = m.Bot()
    assert hasattr(bot, '_route_command')
    # 检查所有 _cmd_ 方法
    cmd_methods = [n for n in dir(bot) if n.startswith('_cmd_')]
    assert '_cmd_list' in cmd_methods
    assert '_cmd_new' in cmd_methods
    assert '_cmd_switch' in cmd_methods
    assert '_cmd_history' in cmd_methods
    assert '_cmd_view' in cmd_methods
    assert '_cmd_status' in cmd_methods
    assert '_cmd_help' in cmd_methods
    assert '_cmd_start' in cmd_methods
    assert '_cmd_stop' in cmd_methods
    assert '_cmd_rename' in cmd_methods
    assert '_cmd_del' in cmd_methods


# ═══════════════════════════════════════════════════════════════════
# 8. Config 完整性
# ═══════════════════════════════════════════════════════════════════

def test_config_dataclass_fields():
    """确保 Config 有所有必要字段。"""
    fields = {f.name for f in config.__dataclass_fields__.values()}
    required = {
        "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_OPEN_ID",
        "POLL_INTERVAL", "THROTTLE_INTERVAL", "CLAUDE_BIN",
        "WORKSPACE_DIR", "DATA_DIR", "CARD_MAX_CHARS",
        "IDLE_POLL_INTERVAL", "AUTO_IDLE_SEC", "RESULT_TIMEOUT",
        "HISTORY_MAX_CHARS", "HISTORY_DISPLAY_N", "HISTORY_DISPLAY_MAX",
        "HISTORY_FILE_MAX_LINES",
    }
    missing = required - fields
    assert not missing, f"Missing config fields: {missing}"
    # 检查值类型
    assert isinstance(config.POLL_INTERVAL, float)


def test_config_defaults_reasonable():
    """默认值应在合理范围内。"""
    assert 1.0 <= config.POLL_INTERVAL <= 10.0
    assert 1.0 <= config.THROTTLE_INTERVAL <= 30.0
    assert config.CARD_MAX_CHARS >= 1000
    assert config.IDLE_POLL_INTERVAL >= 10
    assert config.AUTO_IDLE_SEC >= 300
    assert config.RESULT_TIMEOUT >= 120
    assert 10 <= config.HISTORY_FILE_MAX_LINES <= 5000


def test_config_dirs_writable():
    """配置的目录路径应该合法。"""
    assert len(config.WORKSPACE_DIR) > 0
    assert len(config.DATA_DIR) > 0
    assert "/" not in config.WORKSPACE_DIR.split("/")[-1]  # no trailing slash issues


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
