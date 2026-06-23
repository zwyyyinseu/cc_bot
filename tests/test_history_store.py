"""
history_store 单元测试 —— 测试 JSONL 读写、截断、导入逻辑。
不依赖 Claude 进程或飞书 API。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 将 cc_bot 加入 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config


def setup_module():
    """使用临时目录替代 DATA_DIR。"""
    global _tmpdir
    _tmpdir = tempfile.mkdtemp()
    config.DATA_DIR = _tmpdir


def teardown_module():
    import shutil
    shutil.rmtree(_tmpdir, ignore_errors=True)


def test_append_and_count():
    from history_store import history_store
    hs = history_store
    hs.delete("test_conv")
    assert hs.count("test_conv") == 0
    hs.append("test_conv", "user", "hello")
    assert hs.count("test_conv") == 1
    hs.append("test_conv", "assistant", "hi there")
    assert hs.count("test_conv") == 2


def test_empty_skipped():
    from history_store import history_store
    hs = history_store
    hs.delete("test_empty")
    hs.append("test_empty", "user", "   ")
    assert hs.count("test_empty") == 0


def test_get_recent():
    from history_store import history_store
    hs = history_store
    hs.delete("test_recent")
    for i in range(20):
        hs.append("test_recent", "user", f"msg {i}")
    entries = hs.get_recent("test_recent", n=5)
    assert len(entries) == 5
    assert entries[-1]["text"] == "msg 19"
    assert entries[0]["text"] == "msg 15"
    # 验证格式
    for e in entries:
        assert "role" in e
        assert "text" in e
        assert "timestamp" in e


def test_get_recent_empty():
    from history_store import history_store
    hs = history_store
    assert hs.get_recent("nonexistent_conv") == []
    assert hs.count("nonexistent_conv") == 0


def test_delete():
    from history_store import history_store
    hs = history_store
    hs.append("test_del", "user", "will be deleted")
    assert hs.count("test_del") == 1
    hs.delete("test_del")
    assert hs.count("test_del") == 0


def test_truncation():
    """测试历史文件超过上限时自动截断。"""
    from history_store import history_store
    hs = history_store
    hs.delete("test_trunc")
    limit = config.HISTORY_FILE_MAX_LINES  # 500
    # 写入 2x + 10 条（会触发多次截断）
    for i in range(limit * 2 + 50):
        hs.append("test_trunc", "user", f"line {i}")
    # 截断后应 ≤ 上限（最后一批未触发截断时可能略超，但不超过上限+50）
    count = hs.count("test_trunc")
    assert count <= limit * 2, f"expected <={limit * 2}, got {count}"
    entries = hs.get_recent("test_trunc", n=5)
    # 最近的条目应该在最后一批里
    assert "line" in entries[-1]["text"]
    assert int(entries[-1]["text"].split()[-1]) >= limit * 2


def test_import_from_claude():
    """测试从 Claude session JSONL 文件导入。
    如果有存在的 session 文件则用真实数据测试，否则跳过。
    """
    from history_store import history_store, _claude_session_path
    from pathlib import Path as P
    hs = history_store
    # 查找任意一个存在的 session 文件
    projects_dir = P.home() / ".claude" / "projects"
    session_path = None
    workspace = ""
    if projects_dir.exists():
        for jsonl in projects_dir.glob("*/*.jsonl"):
            if jsonl.stat().st_size > 1000:  # 至少有内容的文件
                session_path = jsonl
                session_id = jsonl.stem
                # 反推 workspace 路径
                proj_dir = jsonl.parent.name  # e.g. "-home-zzzwy-workspace-cc_bot-workspace"
                workspace = "/" + proj_dir.lstrip("-").replace("-", "/")
                break
    if not session_path:
        pytest.skip("no Claude session file found for import test")
        return
    hs.delete("test_import")
    imported = hs.import_from_claude("test_import", workspace, session_id)
    assert imported > 0, f"imported {imported} entries"
    count = hs.count("test_import")
    assert count == imported
    # 验证导入的内容有 user 和 assistant
    entries = hs.get_recent("test_import", n=10)
    roles = {e["role"] for e in entries}
    assert "user" in roles, f"roles: {roles}"
    assert "assistant" in roles, f"roles: {roles}"
    # 不应有错误占位
    for e in entries:
        assert e["text"] != "Request timed out"
    # 再次导入应该跳过
    imported2 = hs.import_from_claude("test_import", workspace, session_id)
    assert imported2 == 0, "second import should skip"
    hs.delete("test_import")


def test_config_values():
    """验证配置值在合理范围内。"""
    assert config.IDLE_POLL_INTERVAL >= 10
    assert config.AUTO_IDLE_SEC >= 300
    assert config.RESULT_TIMEOUT >= 120
    assert config.HISTORY_FILE_MAX_LINES >= 100
    assert config.HISTORY_DISPLAY_MAX <= 50
    assert config.CARD_MAX_CHARS >= 1000


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
