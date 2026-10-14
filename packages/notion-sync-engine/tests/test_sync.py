"""Tests for the Notion sync canonical module (text normalization, format
detection/restoration, rich_text splitting and path validation)."""

import json
import os
import time
from unittest.mock import Mock, patch

import pytest

from sync_lib.canonical import (
    canonical,
    compute_hash,
    detect_format,
    normalize_path,
    restore_format,
    split_rich_text,
    validate_path,
)
from sync_lib.executor import Summary, _br_from_update_response, execute_plan
from sync_lib.plan import build_plan
from sync_lib.planner import (
    Action,
    Classification,
    classify_existence,
    classify_file,
    detect_renames,
)
from sync_lib.remote import NotionRemote, RateLimiter, retry_with_backoff
from sync_lib.scanner import LocalSnapshot, scan_local
from sync_lib.state import (
    StateManager,
    acquire_lock,
    cleanup_tmp_files,
    release_lock,
)


def test_canonical_crlf_to_lf():
    assert canonical("line1\r\nline2") == "line1\nline2"


def test_canonical_cr_to_lf():
    assert canonical("line1\rline2") == "line1\nline2"


def test_canonical_mixed_eol():
    assert canonical("line1\r\nline2\rline3\n") == "line1\nline2\nline3"


def test_canonical_strip_trailing_newlines():
    assert canonical("line1\n\n\n") == "line1"


def test_canonical_preserve_other_whitespace():
    assert canonical("line1\t  \nline2") == "line1\t  \nline2"


def test_canonical_empty():
    assert canonical("") == ""


def test_detect_format_utf8_no_bom_crlf():
    raw = b"line1\r\nline2\r\n"
    fmt = detect_format(raw)
    assert fmt == {"encoding": "utf-8", "bom": False, "eol": "crlf", "final_newline": True}


def test_detect_format_utf8_bom_lf():
    raw = b"\xef\xbb\xbfline1\nline2"
    fmt = detect_format(raw)
    assert fmt == {"encoding": "utf-8", "bom": True, "eol": "lf", "final_newline": False}


def test_detect_format_mixed_eol():
    raw = b"line1\r\nline2\nline3\r\n"
    fmt = detect_format(raw)
    assert fmt["eol"] == "crlf"  # 多数决


def test_restore_format_crlf():
    text = "line1\nline2"
    fmt = {"encoding": "utf-8", "bom": False, "eol": "crlf", "final_newline": True}
    assert restore_format(text, fmt) == b"line1\r\nline2\r\n"


def test_restore_format_bom():
    text = "line1\nline2"
    fmt = {"encoding": "utf-8", "bom": True, "eol": "lf", "final_newline": False}
    assert restore_format(text, fmt) == b"\xef\xbb\xbfline1\nline2"


def test_split_rich_text_exact_2000():
    text = "a" * 2000
    chunks = split_rich_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_rich_text_2001():
    text = "a" * 2001
    chunks = split_rich_text(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == 2000
    assert len(chunks[1]) == 1


def test_split_rich_text_emoji_boundary():
    # emoji 是代理对（2 个 UTF-16 码元）
    text = "a" * 1999 + "😀" + "b" * 100
    chunks = split_rich_text(text)
    assert len(chunks) == 2
    assert chunks[0].endswith("😀") or chunks[1].startswith("😀")  # 不能切碎
    assert "".join(chunks) == text


def test_split_rich_text_chinese():
    # 中文是 1 个 UTF-16 码元
    text = "汉" * 2001
    chunks = split_rich_text(text)
    assert len(chunks) == 2
    assert "".join(chunks) == text


def test_split_rich_text_empty():
    assert split_rich_text("") == [""]


def test_validate_path_normal():
    assert validate_path("src/main.py") is True


def test_validate_path_nested():
    assert validate_path("docs/reports/final.md") is True


def test_validate_path_reject_parent():
    assert validate_path("../etc/passwd") is False


def test_validate_path_reject_absolute():
    assert validate_path("/etc/passwd") is False
    assert validate_path("C:\\Windows\\System32") is False


def test_validate_path_reject_windows_reserved():
    assert validate_path("CON") is False
    assert validate_path("aux.txt") is False
    assert validate_path("COM1") is False
    assert validate_path("LPT9.md") is False


def test_validate_path_reject_trailing_space():
    assert validate_path("file .txt") is False
    assert validate_path("dir /file.txt") is False


def test_validate_path_reject_trailing_dot():
    assert validate_path("file.txt.") is False
    assert validate_path("dir./file.txt") is False


def test_validate_path_reject_backslash_parent():
    assert validate_path("..\\..\\etc\\passwd") is False


def test_validate_path_reject_backslash_parent_mixed():
    assert validate_path("..\\etc/passwd") is False


def test_validate_path_normal_backslash():
    assert validate_path("src\\main.py") is True


def test_validate_path_nfc_nfd_consistent():
    # NFC 与 NFD 形式的同一路径都应被接受
    assert validate_path("café/notes.txt") is True
    assert validate_path("café/notes.txt") is True


def test_normalize_path_posix_separators():
    assert normalize_path("docs\\reports\\final.md") == "docs/reports/final.md"


def test_normalize_path_nfc():
    # NFD 的 "café" 规范化为 NFC
    assert normalize_path("café.txt") == "café.txt"


# --- planner: 内容分类表（6.2 节） ---


def test_classify_skip():
    state = {"base_local_hash": "a", "base_remote_hash": "b", "status": "synced"}
    assert classify_file(state, "a", "b", True, True).action == Action.SKIP


def test_classify_push():
    state = {"base_local_hash": "a", "base_remote_hash": "b", "status": "synced"}
    assert classify_file(state, "c", "b", True, True).action == Action.PUSH


def test_classify_pull():
    state = {"base_local_hash": "a", "base_remote_hash": "b", "status": "synced"}
    assert classify_file(state, "a", "c", True, True).action == Action.PULL


def test_classify_baseline():
    state = {"base_local_hash": "a", "base_remote_hash": "b", "status": "synced"}
    assert classify_file(state, "c", "c", True, True).action == Action.BASELINE


def test_classify_conflict():
    state = {"base_local_hash": "a", "base_remote_hash": "b", "status": "synced"}
    assert classify_file(state, "c", "d", True, True).action == Action.CONFLICT


def test_classify_blocked_invalid_structure():
    state = {"base_local_hash": "a", "base_remote_hash": "b", "status": "synced"}
    assert classify_file(state, "c", "b", True, False).action == Action.BLOCKED


def test_classify_blocked_conflict_status():
    state = {"base_local_hash": "a", "base_remote_hash": "b", "status": "conflict"}
    assert classify_file(state, "c", "b", True, True).action == Action.BLOCKED


# --- planner: 存在性分类表（6.3 节） ---


def test_existence_create_remote():
    # 状态无记录，本地存在，远端不存在
    assert classify_existence(False, True, False, None, None).action == Action.CREATE_REMOTE


def test_existence_adopt_same_content():
    # 状态无记录，本地存在，远端按 Path 查到，内容相同
    assert classify_existence(False, True, True, "a", "a").action == Action.ADOPT


def test_existence_adopt_conflict():
    # 状态无记录，本地存在，远端按 Path 查到，内容不同
    assert classify_existence(False, True, True, "a", "b").action == Action.CONFLICT


def test_existence_unmanaged():
    # 状态无记录，本地不存在，远端存在
    assert classify_existence(False, False, True, None, "b").action == Action.UNMANAGED


def test_existence_archive_remote():
    # 状态有记录，本地删除，远端存在且未改
    assert classify_existence(True, False, True, None, "b", "b").action == Action.ARCHIVE_REMOTE


def test_existence_conflict_local_deleted_remote_changed():
    # 状态有记录，本地删除，远端改了
    assert classify_existence(True, False, True, None, "c", "b").action == Action.CONFLICT


def test_existence_delete_local():
    # 状态有记录，本地存在，远端删除，本地未改
    assert classify_existence(True, True, False, "a", None, None, "a").action == Action.DELETE_LOCAL


def test_existence_conflict_remote_deleted_local_changed():
    # 状态有记录，本地存在，远端删除，本地改了
    assert classify_existence(True, True, False, "c", None, None, "a").action == Action.CONFLICT


def test_existence_both_deleted():
    # 状态有记录，本地删除，远端删除
    assert classify_existence(True, False, False, None, None).action == Action.DELETE_STATE


def test_existence_untracked():
    # 状态有记录，本地不在范围内
    assert classify_existence(True, "out-of-scope", True, None, "b").action == Action.UNTRACKED


# --- planner: 重命名检测（6.4 节） ---


def test_detect_renames_exact_match():
    state = {"old.py": {"base_local_hash": "abc", "page_id": "123"}}
    local_snapshot = {"new.py": {"hash": "abc"}}
    renames = detect_renames(state, local_snapshot, set())
    assert len(renames) == 1
    assert renames[0].old_path == "old.py"
    assert renames[0].new_path == "new.py"


def test_detect_renames_case_only():
    state = {"README.md": {"base_local_hash": "abc", "page_id": "123"}}
    local_snapshot = {"readme.md": {"hash": "abc"}}
    renames = detect_renames(state, local_snapshot, set())
    assert len(renames) == 1
    assert renames[0].old_path == "README.md"
    assert renames[0].new_path == "readme.md"


def test_detect_renames_no_match():
    state = {"old.py": {"base_local_hash": "abc", "page_id": "123"}}
    local_snapshot = {"new.py": {"hash": "xyz"}}
    renames = detect_renames(state, local_snapshot, set())
    assert len(renames) == 0


def test_detect_renames_multiple_matches():
    state = {"old.py": {"base_local_hash": "abc", "page_id": "123"}}
    local_snapshot = {"new1.py": {"hash": "abc"}, "new2.py": {"hash": "abc"}}
    renames = detect_renames(state, local_snapshot, set())
    assert len(renames) == 0  # 多个匹配时不重命名


def test_detect_renames_remote_changed():
    state = {"old.py": {"base_local_hash": "abc", "page_id": "123"}}
    local_snapshot = {"new.py": {"hash": "abc"}}
    remote_changed = {"old.py"}  # 远端已修改
    renames = detect_renames(state, local_snapshot, remote_changed)
    assert len(renames) == 0  # 远端修改时不重命名


# --- state: 状态文件原子读写 ---


def test_state_atomic_write(tmp_path):
    state_file = tmp_path / ".sync-state.json"
    mgr = StateManager(tmp_path)
    mgr.state = {"schema_version": 2, "files": {"test.py": {"page_id": "123"}}}
    mgr.save()

    # 验证文件存在且内容正确
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["schema_version"] == 2
    assert data["files"]["test.py"]["page_id"] == "123"

    # 验证临时文件已清理
    assert not (tmp_path / ".sync-state.json.sync-tmp").exists()


def test_state_load_nonexistent(tmp_path):
    mgr = StateManager(tmp_path)
    mgr.load()
    assert mgr.state == {"schema_version": 2, "files": {}, "tombstones": {}, "skipped": []}


def test_state_load_existing(tmp_path):
    state_file = tmp_path / ".sync-state.json"
    state_file.write_text(json.dumps({"schema_version": 2, "files": {"a.py": {"page_id": "1"}}}))
    mgr = StateManager(tmp_path)
    mgr.load()
    assert mgr.state["files"]["a.py"]["page_id"] == "1"


# --- state: pending 意图日志 ---


def test_state_set_pending(tmp_path):
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("test.py", {"page_id": "123", "base_local_hash": "abc"})
    mgr.set_pending("test.py", "push", "abc")

    entry = mgr.get_file("test.py")
    assert entry["pending"]["op"] == "push"
    assert entry["pending"]["content_hash"] == "abc"
    assert "started_at" in entry["pending"]


def test_state_clear_pending(tmp_path):
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("test.py", {"page_id": "123"})
    mgr.set_pending("test.py", "push", "abc")
    mgr.clear_pending("test.py")

    entry = mgr.get_file("test.py")
    assert entry["pending"] is None


def test_state_recover_pending_create_no_page_id(tmp_path):
    """pending op=create 但无 page_id → 按 Path 查询"""
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("test.py", {"pending": {"op": "create", "content_hash": "abc"}})

    recovered = mgr.recover_pending()
    assert len(recovered) == 1
    assert recovered[0]["path"] == "test.py"
    assert recovered[0]["action"] == "adopt_or_skip"


# --- state: 进程锁与清理 ---


def test_acquire_lock_success(tmp_path):
    lock_file = tmp_path / ".sync.lock"
    assert acquire_lock(tmp_path) is True
    assert lock_file.exists()
    release_lock(tmp_path)
    assert not lock_file.exists()


def test_acquire_lock_already_locked(tmp_path):
    lock_file = tmp_path / ".sync.lock"
    # 写入一个存活进程的 PID
    lock_file.write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-10-14T00:00:00Z"}))
    assert acquire_lock(tmp_path) is False


def test_acquire_lock_stale(tmp_path):
    lock_file = tmp_path / ".sync.lock"
    # 写入一个不存在的 PID
    lock_file.write_text(json.dumps({"pid": 99999, "started_at": "2026-10-14T00:00:00Z"}))
    assert acquire_lock(tmp_path) is True  # 锁过期，可以获取


def test_cleanup_tmp_files(tmp_path):
    (tmp_path / "file1.sync-tmp").write_text("tmp")
    (tmp_path / "file2.sync-tmp").write_text("tmp")
    (tmp_path / "normal.txt").write_text("keep")
    cleanup_tmp_files(tmp_path)
    assert not (tmp_path / "file1.sync-tmp").exists()
    assert not (tmp_path / "file2.sync-tmp").exists()
    assert (tmp_path / "normal.txt").exists()


# --- scanner: 本地文件扫描 ---


def test_scanner_include_exclude(tmp_path):
    # 创建测试文件
    (tmp_path / "README.md").write_text("test")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("test")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "old.py").write_text("test")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("test")

    config = {
        "include": ["*.md", "src/**/*.py"],
        "exclude": ["artifacts/", ".*/"],
    }

    snapshot = scan_local(tmp_path, config)
    assert "README.md" in snapshot
    assert "src/main.py" in snapshot
    assert "artifacts/old.py" not in snapshot
    assert ".git/config" not in snapshot


def test_scanner_detect_format(tmp_path):
    (tmp_path / "test.md").write_bytes(b"\xef\xbb\xbfline1\r\nline2\r\n")
    config = {"include": ["*.md"], "exclude": []}
    snapshot = scan_local(tmp_path, config)

    entry = snapshot["test.md"]
    assert entry["format"]["bom"] is True
    assert entry["format"]["eol"] == "crlf"
    assert entry["format"]["final_newline"] is True


def test_scanner_skip_large_file(tmp_path):
    large_content = "a" * (101 * 1024)  # 101 KB
    (tmp_path / "large.md").write_text(large_content)
    config = {"include": ["*.md"], "exclude": [], "max_chars": 100000}
    snapshot = scan_local(tmp_path, config)

    assert "large.md" not in snapshot
    assert len(snapshot.skipped) == 1
    assert snapshot.skipped[0]["path"] == "large.md"
    assert snapshot.skipped[0]["reason"] == "max_chars_exceeded"


def test_scanner_path_normalization(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("test")
    config = {"include": ["docs/**/*.md"], "exclude": []}
    snapshot = scan_local(tmp_path, config)

    # 路径应该是 POSIX 格式
    assert "docs/guide.md" in snapshot
    assert "docs\\guide.md" not in snapshot


def test_scanner_path_nfc_normalization(tmp_path):
    # NFD 组合文件名（e + U+0301）
    nfd_name = "café.md"
    (tmp_path / nfd_name).write_text("test")
    config = {"include": ["*.md"], "exclude": []}
    snapshot = scan_local(tmp_path, config)

    # 快照键应经 normalize_path 规范化为 NFC 形式
    assert "café.md" in snapshot
    assert nfd_name not in snapshot


# --- remote: 令牌桶限流器 ---


def test_rate_limiter_basic():
    limiter = RateLimiter(rate=2.5)  # 2.5 req/s
    start = time.time()
    for _ in range(3):
        limiter.wait()
    elapsed = time.time() - start
    # 3 个请求至少需要 (3-1) / 2.5 = 0.8 秒
    assert elapsed >= 0.8


def test_rate_limiter_burst():
    limiter = RateLimiter(rate=10.0, burst=3)
    start = time.time()
    for _ in range(3):
        limiter.wait()
    elapsed = time.time() - start
    # 突发 3 个应该立即完成
    assert elapsed < 0.1


# --- remote: 指数退避重试 ---


def test_retry_success_first_try():
    func = Mock(return_value="success")
    result = retry_with_backoff(func, max_retries=3)
    assert result == "success"
    assert func.call_count == 1


def test_retry_success_after_failure():
    func = Mock(side_effect=[Exception("fail"), Exception("fail"), "success"])
    result = retry_with_backoff(func, max_retries=3)
    assert result == "success"
    assert func.call_count == 3


def test_retry_exhausted():
    func = Mock(side_effect=Exception("fail"))
    with pytest.raises(Exception):
        retry_with_backoff(func, max_retries=2)
    assert func.call_count == 2


def test_retry_respects_retry_after():
    # 内置 Exception 不接受关键字参数，先构造再挂 headers 属性
    err = Exception("429")
    err.headers = {"Retry-After": "2"}
    func = Mock(side_effect=[err, "success"])
    with patch("time.sleep") as mock_sleep:
        result = retry_with_backoff(func, max_retries=2)
        assert result == "success"
        mock_sleep.assert_called_with(2)


# --- remote: code block 读写（mock Notion API） ---


def test_create_page_with_code_block():
    mock_client = Mock()
    mock_client.pages.create.return_value = {"id": "page123"}

    remote = NotionRemote(mock_client, "db123", "ds123")
    page_id = remote.create_page(
        path="test.md",
        content="# Hello",
        file_type="markdown",
        directory="",
        chars=7,
    )

    assert page_id == "page123"
    mock_client.pages.create.assert_called_once()
    call_args = mock_client.pages.create.call_args[1]
    assert call_args["parent"]["data_source_id"] == "ds123"
    assert call_args["properties"]["Name"]["title"][0]["text"]["content"] == "test.md"


def test_read_page_single_code_block():
    mock_client = Mock()
    mock_client.blocks.children.list.return_value = {
        "results": [{
            "id": "block123",
            "type": "code",
            "code": {
                "rich_text": [{"plain_text": "line1\n"}, {"plain_text": "line2"}],
                "language": "markdown",
            },
        }],
        "has_more": False,
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    content, block_id, struct_valid = remote.read_page("page123")

    assert content == "line1\nline2"
    assert block_id == "block123"
    assert struct_valid is True


def test_read_page_invalid_structure_multiple_blocks():
    mock_client = Mock()
    mock_client.blocks.children.list.return_value = {
        "results": [
            {"id": "block1", "type": "code", "code": {"rich_text": []}},
            {"id": "block2", "type": "code", "code": {"rich_text": []}},
        ],
        "has_more": False,
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    content, block_id, struct_valid = remote.read_page("page123")

    assert struct_valid is False


def test_update_block_splits_rich_text():
    mock_client = Mock()
    mock_client.blocks.update.return_value = {"last_edited_time": "2026-10-14T12:00:00Z"}

    remote = NotionRemote(mock_client, "db123", "ds123")
    result = remote.update_block("block123", "a" * 2001)

    assert result == {"last_edited_time": "2026-10-14T12:00:00Z"}
    mock_client.blocks.update.assert_called_once()
    call_args = mock_client.blocks.update.call_args[1]
    assert call_args["block_id"] == "block123"
    rich_text = call_args["code"]["rich_text"]
    assert len(rich_text) == 2
    assert rich_text[0]["text"]["content"] == "a" * 2000
    assert rich_text[1]["text"]["content"] == "a"


# --- executor: 执行计划（PUSH/PULL + 乐观并发校验） ---


def test_execute_push(tmp_path):
    # 准备
    (tmp_path / "test.md").write_text("new content")

    state_mgr = Mock()
    remote = Mock()
    remote.read_page.return_value = ("old content", "block123", True)
    remote.update_block.return_value = {"last_edited_time": "2026-10-14T12:00:00Z"}

    # 最小改动：Mock 的 get_file 默认返回 Mock，无法下标访问；
    # 设为真实 dict 使写前校验通过（见 brief 授权）
    state_mgr.get_file.return_value = {
        "page_id": "p1",
        "block_id": "b1",
        "base_remote_hash": compute_hash("old content"),
        "format": {"encoding": "utf-8", "bom": False, "eol": "lf", "final_newline": False},
    }

    plan = {"test.md": Classification(Action.PUSH)}

    # 执行
    summary = execute_plan(plan, state_mgr, remote, tmp_path, {})

    # 验证
    assert summary.pushed == 1
    remote.update_block.assert_called_once()
    state_mgr.set_pending.assert_called_once()
    state_mgr.clear_pending.assert_called_once()


# --- remote: create_database（前置补充，Task 8） ---


def test_create_database_returns_ids():
    mock_client = Mock()
    mock_client.databases.create.return_value = {
        "id": "db123",
        "data_sources": [{"id": "ds123", "name": "Notion Sync"}],
    }

    remote = NotionRemote(mock_client, None, None)
    database_id, data_source_id = remote.create_database("page123")

    assert database_id == "db123"
    assert data_source_id == "ds123"
    mock_client.databases.create.assert_called_once()
    call_args = mock_client.databases.create.call_args[1]
    assert call_args["parent"] == {"type": "page_id", "page_id": "page123"}
    assert call_args["initial_data_source"]["properties"]["Name"]["title"] == {}


def test_create_database_uses_rate_limiter():
    mock_client = Mock()
    mock_client.databases.create.return_value = {
        "id": "db123",
        "data_sources": [{"id": "ds123", "name": "Notion Sync"}],
    }

    remote = NotionRemote(mock_client, None, None)
    remote.limiter = Mock()

    remote.create_database("page123")

    remote.limiter.wait.assert_called_once()


def test_create_database_declares_all_five_properties():
    mock_client = Mock()
    mock_client.databases.create.return_value = {
        "id": "db123",
        "data_sources": [{"id": "ds123", "name": "Notion Sync"}],
    }

    remote = NotionRemote(mock_client, None, None)
    remote.create_database("page123")

    props = mock_client.databases.create.call_args[1]["initial_data_source"]["properties"]
    assert set(props) == {"Name", "Path", "Type", "Directory", "Chars"}
    assert props["Name"] == {"title": {}}
    assert props["Path"] == {"rich_text": {}}
    assert props["Directory"] == {"rich_text": {}}
    assert props["Chars"] == {"number": {}}
    assert set(props["Type"]) == {"select"}
    option_names = [opt["name"] for opt in props["Type"]["select"]["options"]]
    assert set(option_names) == {"markdown", "python", "yaml", "toml", "text"}


# --- cli: 命令入口（Task 8） ---


def test_cli_init(tmp_path, monkeypatch):
    from sync_lib.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "test_token")

    with patch("sync_lib.cli.NotionRemote") as mock_remote:
        mock_remote.return_value.create_database.return_value = ("db123", "ds123")

        with patch("sys.argv", ["sync.py", "init", "--parent-page-id", "page123"]):
            main()

        # 验证配置文件生成
        assert (tmp_path / ".sync-config.yaml").exists()
        assert (tmp_path / ".sync-state.json").exists()


# --- remote: 远端读取（cmd_sync 主线 Task A） ---


def _page(page_id, path, last_edited="2026-10-14T10:00:00Z", in_trash=False):
    """构造 data_sources.query 返回的 page 对象"""
    props = {"Path": {"rich_text": [{"plain_text": path}]}} if path is not None else {}
    return {
        "id": page_id,
        "last_edited_time": last_edited,
        "in_trash": in_trash,
        "properties": props,
    }


def test_query_candidates_paginates_and_extracts_fields():
    mock_client = Mock()
    mock_client.data_sources.query.side_effect = [
        {
            "results": [
                _page("p1", "a.md"),
                _page("p2", "b/c.py", in_trash=True),
            ],
            "has_more": True,
            "next_cursor": "cursor1",
        },
        {
            "results": [_page("p3", "d.md")],
            "has_more": False,
            "next_cursor": None,
        },
    ]

    remote = NotionRemote(mock_client, "db123", "ds123")
    remote.limiter.wait = Mock()

    result = remote.query_candidates(None)

    assert result == [
        {"id": "p1", "path": "a.md",
         "last_edited_time": "2026-10-14T10:00:00Z", "in_trash": False},
        {"id": "p2", "path": "b/c.py",
         "last_edited_time": "2026-10-14T10:00:00Z", "in_trash": True},
        {"id": "p3", "path": "d.md",
         "last_edited_time": "2026-10-14T10:00:00Z", "in_trash": False},
    ]
    assert mock_client.data_sources.query.call_count == 2
    # 第一页无 cursor，第二页带上 next_cursor
    first_call = mock_client.data_sources.query.call_args_list[0][1]
    second_call = mock_client.data_sources.query.call_args_list[1][1]
    assert "start_cursor" not in first_call
    assert second_call["start_cursor"] == "cursor1"
    # 每页都走限流
    assert remote.limiter.wait.call_count == 2


def test_query_candidates_with_since_adds_filter():
    mock_client = Mock()
    mock_client.data_sources.query.return_value = {
        "results": [_page("p1", "a.md")],
        "has_more": False,
        "next_cursor": None,
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    remote.limiter.wait = Mock()

    remote.query_candidates("2026-10-01T00:00:00Z")

    call_args = mock_client.data_sources.query.call_args[1]
    assert call_args["filter"] == {
        "timestamp": "last_edited_time",
        "last_edited_time": {"on_or_after": "2026-10-01T00:00:00Z"},
    }


def test_query_candidates_missing_path_is_none():
    mock_client = Mock()
    mock_client.data_sources.query.return_value = {
        "results": [_page("p1", None)],
        "has_more": False,
        "next_cursor": None,
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    remote.limiter.wait = Mock()

    result = remote.query_candidates(None)

    assert result[0]["path"] is None


def test_query_candidates_uses_retry_and_data_source_id():
    mock_client = Mock()
    mock_client.data_sources.query.return_value = {
        "results": [],
        "has_more": False,
        "next_cursor": None,
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    remote.limiter.wait = Mock()

    with patch("sync_lib.remote.retry_with_backoff", side_effect=lambda f: f()) as mock_retry:
        remote.query_candidates(None)

    assert mock_retry.call_count == 1
    assert mock_client.data_sources.query.call_args[0][0] == "ds123"


def test_query_by_paths_returns_hits_only():
    mock_client = Mock()

    def query(data_source_id, **kwargs):
        path = kwargs["filter"]["rich_text"]["equals"]
        if path == "a.md":
            return {"results": [_page("p1", "a.md")], "has_more": False}
        return {"results": [], "has_more": False}

    mock_client.data_sources.query.side_effect = query

    remote = NotionRemote(mock_client, "db123", "ds123")
    remote.limiter.wait = Mock()

    result = remote.query_by_paths(["a.md", "missing.md"])

    assert result == {"a.md": "p1"}
    assert "missing.md" not in result
    # 逐个走限流
    assert remote.limiter.wait.call_count == 2
    # filter 体按 Path 精确匹配
    first_call = mock_client.data_sources.query.call_args_list[0][1]
    assert first_call["filter"] == {"property": "Path", "rich_text": {"equals": "a.md"}}


def test_archive_page_sets_in_trash():
    mock_client = Mock()
    mock_client.pages.update.return_value = {"id": "p1", "in_trash": True}

    remote = NotionRemote(mock_client, "db123", "ds123")
    remote.limiter.wait = Mock()

    remote.archive_page("p1")

    mock_client.pages.update.assert_called_once_with(page_id="p1", in_trash=True)
    assert remote.limiter.wait.call_count == 1


def test_read_page_collects_all_pages():
    mock_client = Mock()
    mock_client.blocks.children.list.side_effect = [
        {
            "results": [],
            "has_more": True,
            "next_cursor": "cursor1",
        },
        {
            "results": [{
                "id": "block123",
                "type": "code",
                "code": {"rich_text": [{"plain_text": "line1\n"}, {"plain_text": "line2"}]},
            }],
            "has_more": False,
            "next_cursor": None,
        },
    ]

    remote = NotionRemote(mock_client, "db123", "ds123")
    remote.limiter.wait = Mock()

    content, block_id, struct_valid = remote.read_page("page123")

    assert content == "line1\nline2"
    assert block_id == "block123"
    assert struct_valid is True
    assert mock_client.blocks.children.list.call_count == 2
    second_call = mock_client.blocks.children.list.call_args_list[1][1]
    assert second_call["start_cursor"] == "cursor1"


def test_read_page_multi_page_invalid_structure():
    # 第二页多出一个非 code block → 结构校验失败
    mock_client = Mock()
    mock_client.blocks.children.list.side_effect = [
        {"results": [{"id": "b1", "type": "code", "code": {"rich_text": []}}],
         "has_more": True, "next_cursor": "c1"},
        {"results": [{"id": "b2", "type": "paragraph", "paragraph": {"rich_text": []}}],
         "has_more": False, "next_cursor": None},
    ]

    remote = NotionRemote(mock_client, "db123", "ds123")
    remote.limiter.wait = Mock()

    content, block_id, struct_valid = remote.read_page("page123")

    assert struct_valid is False


# --- remote: 更新页面属性（cmd_sync 主线 Task B，RENAME 用） ---


def test_update_page_properties_calls_pages_update():
    mock_client = Mock()
    mock_client.pages.update.return_value = {"id": "p1"}

    remote = NotionRemote(mock_client, "db123", "ds123")
    remote.limiter.wait = Mock()

    props = {"Name": "new.py", "Path": "new.py", "Directory": "src"}
    remote.update_page_properties("p1", props)

    mock_client.pages.update.assert_called_once_with(page_id="p1", properties=props)
    assert remote.limiter.wait.call_count == 1


# --- executor: BR 取写响应体（设计 5.5，H-I-1） ---


def test_br_from_update_response_valid():
    resp = {
        "id": "b1",
        "type": "code",
        "code": {"rich_text": [{"plain_text": "line1\n"}, {"plain_text": "line2"}]},
    }
    assert _br_from_update_response(resp, "b1", "line1\nline2") == compute_hash("line1\nline2")


def test_br_from_update_response_wrong_type():
    resp = {"id": "b1", "type": "paragraph", "code": {"rich_text": [{"plain_text": "x"}]}}
    assert _br_from_update_response(resp, "b1", "x") is None


def test_br_from_update_response_wrong_id():
    resp = {"id": "other", "type": "code", "code": {"rich_text": [{"plain_text": "x"}]}}
    assert _br_from_update_response(resp, "b1", "x") is None


def test_br_from_update_response_rich_text_not_list():
    resp = {"id": "b1", "type": "code", "code": {"rich_text": "x"}}
    assert _br_from_update_response(resp, "b1", "x") is None


def test_br_from_update_response_missing_plain_text():
    resp = {"id": "b1", "type": "code", "code": {"rich_text": [{"text": {"content": "x"}}]}}
    assert _br_from_update_response(resp, "b1", "x") is None


# --- executor: 全部动作（cmd_sync 主线 Task B） ---


def test_execute_push_uses_response_for_br(tmp_path):
    """H-I-1：PUSH 后 BR 取写响应体，而非直接沿用本地 hash。"""
    (tmp_path / "test.md").write_text("new content")

    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("test.md", {
        "page_id": "p1", "block_id": "b1",
        "base_remote_hash": compute_hash("old content"),
    })

    remote = Mock()
    remote.read_page.return_value = ("old content", "b1", True)
    remote.update_block.return_value = {
        "id": "b1",
        "type": "code",
        "code": {"rich_text": [{"plain_text": "new content"}]},
    }

    plan = {"test.md": Classification(Action.PUSH)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.pushed == 1
    entry = mgr.get_file("test.md")
    assert entry["base_remote_hash"] == compute_hash("new content")
    assert entry["base_local_hash"] == compute_hash("new content")


def test_execute_push_br_fallback_readback(tmp_path):
    """写响应不完整时回退读回远端重算 BR。"""
    (tmp_path / "test.md").write_text("new content")

    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("test.md", {
        "page_id": "p1", "block_id": "b1",
        "base_remote_hash": compute_hash("old content"),
    })

    remote = Mock()
    # 第一次：写前校验；第二次：写响应不可用后的读回
    remote.read_page.side_effect = [
        ("old content", "b1", True),
        ("new content", "b1", True),
    ]
    remote.update_block.return_value = {
        "id": "b1",
        "type": "code",
        "code": {"rich_text": [{"text": {"content": "new content"}}]},  # 无 plain_text
    }

    plan = {"test.md": Classification(Action.PUSH)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.pushed == 1
    assert mgr.get_file("test.md")["base_remote_hash"] == compute_hash("new content")


def test_execute_push_valid_false_records_error(tmp_path):
    """H-I-2：写前校验结构异常时不崩溃，记入 errors，且不调用 update_block。"""
    (tmp_path / "test.md").write_text("new content")

    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("test.md", {
        "page_id": "p1", "block_id": "b1",
        "base_remote_hash": compute_hash("old content"),
    })

    remote = Mock()
    remote.read_page.return_value = (None, None, False)

    plan = {"test.md": Classification(Action.PUSH)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.pushed == 0
    assert len(summary.errors) == 1
    assert "invalid-structure" in summary.errors[0]["error"]
    remote.update_block.assert_not_called()


def test_execute_push_optimistic_conflict(tmp_path):
    """6.6：写前校验发现远端已改 → 中止，不写入。"""
    (tmp_path / "test.md").write_text("new content")

    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("test.md", {
        "page_id": "p1", "block_id": "b1",
        "base_remote_hash": compute_hash("old content"),
    })

    remote = Mock()
    remote.read_page.return_value = ("changed remotely", "b1", True)

    plan = {"test.md": Classification(Action.PUSH)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.pushed == 0
    assert len(summary.errors) == 1
    remote.update_block.assert_not_called()


def test_execute_pull(tmp_path):
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("a.md", {
        "page_id": "p1", "block_id": "b1",
        "format": {"encoding": "utf-8", "bom": False, "eol": "lf", "final_newline": False},
    })

    remote = Mock()
    remote.read_page.return_value = ("remote text", "b1", True)

    plan = {"a.md": Classification(Action.PULL)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.pulled == 1
    assert (tmp_path / "a.md").read_text() == "remote text"
    entry = mgr.get_file("a.md")
    assert entry["base_local_hash"] == compute_hash("remote text")
    assert entry["base_remote_hash"] == compute_hash("remote text")
    assert entry["block_id"] == "b1"


def test_execute_create_remote(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')")

    mgr = StateManager(tmp_path)
    mgr.load()

    remote = Mock()
    remote.create_page.return_value = "page-new"
    remote.read_page.return_value = ("print('hi')", "block-new", True)

    plan = {"src/main.py": Classification(Action.CREATE_REMOTE)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.created == 1
    args = remote.create_page.call_args[0]
    assert args[0] == "src/main.py"
    assert args[1] == "print('hi')"
    assert args[2] == "python"
    assert args[3] == "src"
    assert args[4] == 11  # len(canonical("print('hi')"))

    entry = mgr.get_file("src/main.py")
    assert entry["page_id"] == "page-new"
    assert entry["block_id"] == "block-new"
    assert entry["base_local_hash"] == compute_hash("print('hi')")
    assert entry["base_remote_hash"] == compute_hash("print('hi')")
    assert entry["status"] == "synced"
    assert entry["format"]["eol"] == "lf"


def test_execute_adopt(tmp_path):
    (tmp_path / "a.md").write_text("same")

    mgr = StateManager(tmp_path)
    mgr.load()

    remote = Mock()
    remote.read_page.return_value = ("same", "b1", True)

    plan = {"a.md": Classification(Action.ADOPT, meta={"page_id": "p1"})}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.adopted == 1
    entry = mgr.get_file("a.md")
    assert entry["page_id"] == "p1"
    assert entry["block_id"] == "b1"
    assert entry["base_remote_hash"] == compute_hash("same")
    assert entry["status"] == "synced"


def test_execute_adopt_mismatch_records_error(tmp_path):
    (tmp_path / "a.md").write_text("local")

    mgr = StateManager(tmp_path)
    mgr.load()

    remote = Mock()
    remote.read_page.return_value = ("remote", "b1", True)

    plan = {"a.md": Classification(Action.ADOPT, meta={"page_id": "p1"})}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.adopted == 0
    assert len(summary.errors) == 1


def test_execute_archive_remote(tmp_path):
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("gone.md", {"page_id": "p1", "base_remote_hash": compute_hash("x")})

    remote = Mock()
    remote.read_page.return_value = ("x", "b1", True)

    plan = {"gone.md": Classification(Action.ARCHIVE_REMOTE)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.archived == 1
    remote.archive_page.assert_called_once_with("p1")
    assert "gone.md" not in mgr.state["files"]
    assert mgr.state["tombstones"]["gone.md"]["page_id"] == "p1"


def test_execute_archive_remote_conflict(tmp_path):
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("gone.md", {"page_id": "p1", "base_remote_hash": compute_hash("x")})

    remote = Mock()
    remote.read_page.return_value = ("changed", "b1", True)

    plan = {"gone.md": Classification(Action.ARCHIVE_REMOTE)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.archived == 0
    assert len(summary.errors) == 1
    remote.archive_page.assert_not_called()


def test_execute_delete_local(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.md").write_text("content")

    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("sub/x.md", {"page_id": "p1"})

    plan = {"sub/x.md": Classification(Action.DELETE_LOCAL)}
    summary = execute_plan(plan, mgr, Mock(), tmp_path, {})

    assert summary.deleted == 1
    assert not (tmp_path / "sub" / "x.md").exists()
    backup = tmp_path / ".sync-backup" / "deleted" / "sub" / "x.md"
    assert backup.read_text() == "content"
    assert "sub/x.md" not in mgr.state["files"]
    assert mgr.state["tombstones"]["sub/x.md"]["page_id"] == "p1"


def test_execute_rename(tmp_path):
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("old.py", {"page_id": "p1", "base_local_hash": "h"})

    remote = Mock()
    remote.update_page_properties.return_value = {"id": "p1"}

    plan = {"old.py": Classification(
        Action.RENAME, meta={"new_path": "src/new.py", "page_id": "p1"})}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.renamed == 1
    args = remote.update_page_properties.call_args[0]
    assert args[0] == "p1"
    assert args[1] == {
        "Name": {"title": [{"text": {"content": "src/new.py"}}]},
        "Path": {"rich_text": [{"text": {"content": "src/new.py"}}]},
        "Directory": {"rich_text": [{"text": {"content": "src"}}]},
    }
    assert "old.py" not in mgr.state["files"]
    assert mgr.state["files"]["src/new.py"]["page_id"] == "p1"


def test_execute_rename_wraps_properties_in_notion_shapes(tmp_path):
    """RENAME 传给 update_page_properties 的属性必须是 Notion 包装形状
    （Name=title、Path/Directory=rich_text），而非裸字符串。"""
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("old.py", {"page_id": "p1", "base_local_hash": "h"})

    remote = Mock()
    remote.update_page_properties.return_value = {"id": "p1"}

    plan = {"old.py": Classification(
        Action.RENAME, meta={"new_path": "src/new.py", "page_id": "p1"})}
    execute_plan(plan, mgr, remote, tmp_path, {})

    properties = remote.update_page_properties.call_args[0][1]
    assert properties["Name"]["title"][0]["text"]["content"] == "src/new.py"
    assert properties["Path"]["rich_text"][0]["text"]["content"] == "src/new.py"
    assert properties["Directory"]["rich_text"][0]["text"]["content"] == "src"


def test_execute_baseline(tmp_path):
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("a.md", {
        "page_id": "p1", "base_local_hash": "old", "base_remote_hash": "old"})

    remote = Mock()
    remote.read_page.return_value = ("same", "b1", True)

    plan = {"a.md": Classification(Action.BASELINE)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.baselined == 1
    entry = mgr.get_file("a.md")
    assert entry["base_local_hash"] == compute_hash("same")
    assert entry["base_remote_hash"] == compute_hash("same")
    assert entry["status"] == "synced"


def test_execute_conflict_backs_up_remote(tmp_path):
    (tmp_path / "a.md").write_text("local version")

    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.update_file("a.md", {"page_id": "p1"})

    remote = Mock()
    remote.read_page.return_value = ("remote version", "b1", True)

    plan = {"a.md": Classification(Action.CONFLICT)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.conflicts == 1
    # 本地不动
    assert (tmp_path / "a.md").read_text() == "local version"
    backups = list((tmp_path / ".sync-backup" / "conflicts").glob("a.md.remote-*.md"))
    assert len(backups) == 1
    assert backups[0].read_text() == "remote version"
    assert mgr.get_file("a.md")["status"] == "conflict"


# --- plan: build_plan 三方合并（6.1 步骤 5） ---


def _snapshot(files):
    """用 {path: hash} 构造 LocalSnapshot。"""
    return LocalSnapshot(
        files={path: {"hash": h, "format": {}, "size_kb": 0.0} for path, h in files.items()}
    )


def _state_entry(page_id="p1", base_local="a", base_remote="b", status="synced"):
    return {
        "page_id": page_id,
        "block_id": "b1",
        "base_local_hash": base_local,
        "base_remote_hash": base_remote,
        "format": {},
        "status": status,
    }


def _remote_entry(page_id="p1", hash="b", struct_valid=True):
    return {
        "page_id": page_id,
        "hash": hash,
        "last_edited_time": "2026-01-01T00:00:00.000Z",
        "struct_valid": struct_valid,
    }


# 6.2 内容分类表（两端都存在的已跟踪文件）
PLAN_62_CASES = [
    # name, local_hash, remote_hash, struct_valid, status, expected
    ("skip", "a", "b", True, "synced", Action.SKIP),
    ("push", "c", "b", True, "synced", Action.PUSH),
    ("pull", "a", "c", True, "synced", Action.PULL),
    ("baseline", "c", "c", True, "synced", Action.BASELINE),
    ("conflict", "c", "d", True, "synced", Action.CONFLICT),
    ("blocked_invalid_structure", "c", "b", False, "synced", Action.BLOCKED),
    ("blocked_conflict_status", "c", "b", True, "conflict", Action.BLOCKED),
]


@pytest.mark.parametrize(
    "name,local_hash,remote_hash,struct_valid,status,expected", PLAN_62_CASES
)
def test_build_plan_62_content_table(name, local_hash, remote_hash, struct_valid, status, expected):
    plan = build_plan(
        _snapshot({"a.md": local_hash}),
        {"a.md": _remote_entry(hash=remote_hash, struct_valid=struct_valid)},
        {"a.md": _state_entry(base_local="a", base_remote="b", status=status)},
        {},
    )
    assert plan["a.md"].action == expected


# 6.3 存在性分类表（删除与新增）
PLAN_63_CASES = [
    # name, path, local_files, remote_index, state_files, config, expected
    ("create_remote", "new.md", {"new.md": "h"}, {}, {}, {}, Action.CREATE_REMOTE),
    (
        "adopt_same",
        "a.md",
        {"a.md": "h"},
        {"a.md": _remote_entry(page_id="p9", hash="h")},
        {},
        {},
        Action.ADOPT,
    ),
    (
        "adopt_conflict",
        "a.md",
        {"a.md": "h1"},
        {"a.md": _remote_entry(hash="h2")},
        {},
        {},
        Action.CONFLICT,
    ),
    ("unmanaged", "x.md", {}, {"x.md": _remote_entry()}, {}, {}, Action.UNMANAGED),
    (
        "archive_remote",
        "a.md",
        {},
        {"a.md": _remote_entry(hash="b")},
        {"a.md": _state_entry(base_local="a", base_remote="b")},
        {},
        Action.ARCHIVE_REMOTE,
    ),
    (
        "conflict_local_deleted_remote_changed",
        "a.md",
        {},
        {"a.md": _remote_entry(hash="c")},
        {"a.md": _state_entry(base_local="a", base_remote="b")},
        {},
        Action.CONFLICT,
    ),
    (
        "delete_local",
        "a.md",
        {"a.md": "a"},
        {},
        {"a.md": _state_entry(base_local="a", base_remote="b")},
        {},
        Action.DELETE_LOCAL,
    ),
    (
        "conflict_remote_deleted_local_changed",
        "a.md",
        {"a.md": "c"},
        {},
        {"a.md": _state_entry(base_local="a", base_remote="b")},
        {},
        Action.CONFLICT,
    ),
    ("delete_state", "a.md", {}, {}, {"a.md": _state_entry()}, {}, Action.DELETE_STATE),
    (
        "untracked",
        "secrets/x.md",
        {},
        {},
        {"secrets/x.md": _state_entry()},
        {"exclude": ["secrets/**"]},
        Action.UNTRACKED,
    ),
]


@pytest.mark.parametrize(
    "name,path,local_files,remote_index,state_files,config,expected", PLAN_63_CASES
)
def test_build_plan_63_existence_table(
    name, path, local_files, remote_index, state_files, config, expected
):
    plan = build_plan(_snapshot(local_files), remote_index, state_files, config)
    assert plan[path].action == expected


def test_build_plan_adopt_carries_page_id():
    plan = build_plan(
        _snapshot({"a.md": "same"}),
        {"a.md": _remote_entry(page_id="p42", hash="same")},
        {},
        {},
    )
    assert plan["a.md"].action == Action.ADOPT
    assert plan["a.md"].meta == {"page_id": "p42"}


def test_build_plan_rename_exact():
    plan = build_plan(
        _snapshot({"new.md": "abc"}),
        {},
        {"old.md": _state_entry(page_id="p7", base_local="abc", base_remote="abc")},
        {},
    )
    assert plan["old.md"].action == Action.RENAME
    assert plan["old.md"].meta == {"new_path": "new.md", "page_id": "p7"}
    assert "new.md" not in plan  # 目标路径已从待分类集合移除


def test_build_plan_rename_case_only():
    plan = build_plan(
        _snapshot({"readme.md": "abc"}),
        {},
        {"README.md": _state_entry(page_id="p7", base_local="abc", base_remote="abc")},
        {},
    )
    assert plan["README.md"].action == Action.RENAME
    assert plan["README.md"].meta["new_path"] == "readme.md"


def test_build_plan_rename_skipped_when_remote_changed():
    plan = build_plan(
        _snapshot({"new.md": "abc"}),
        {"old.md": _remote_entry(hash="changed")},
        {"old.md": _state_entry(page_id="p7", base_local="abc", base_remote="abc")},
        {},
    )
    # 远端已改 → 不判重命名；old.md 走存在性分类（本地消失、远端改了）→ CONFLICT
    assert plan["old.md"].action == Action.CONFLICT
    assert plan["new.md"].action == Action.CREATE_REMOTE


def test_build_plan_normalizes_keys():
    plan = build_plan(
        _snapshot({"sub\\a.md": "a"}),
        {"sub\\a.md": _remote_entry(hash="b")},
        {"sub\\a.md": _state_entry(base_local="a", base_remote="b")},
        {},
    )
    assert "sub/a.md" in plan
    assert plan["sub/a.md"].action == Action.SKIP


def test_build_plan_trashed_remote_treated_as_absent():
    plan = build_plan(
        _snapshot({"a.md": "a"}),
        {"a.md": _remote_entry(hash="b")},
        {"a.md": _state_entry(base_local="a", base_remote="b")},
        {},
        remote_paths_trashed={"a.md"},
    )
    # 远端在回收站 → 视为远端不存在：本地未改 → DELETE_LOCAL
    assert plan["a.md"].action == Action.DELETE_LOCAL


# --- cli: cmd_sync 编排（cmd_sync 主线 Task D） ---


class _Args:
    """cmd_sync 需要的参数命名空间。"""

    def __init__(self, command, dry_run=False, full=False):
        self.command = command
        self.dry_run = dry_run
        self.full = full


def _write_config(root):
    (root / ".sync-config.yaml").write_text(
        "include: ['*.md']\nexclude: []\nremote_overlap_minutes: 5\n"
    )


def _write_state(root, state):
    (root / ".sync-state.json").write_text(json.dumps(state))


def _patch_pipeline(monkeypatch, plan, summary=None, order=None, remote=None):
    """把 cmd_sync 的外部依赖替换为可控 mock，返回 (mocks, remote)。"""
    from sync_lib import cli

    if remote is None:
        remote = Mock()
        remote.query_candidates.return_value = []
        remote.query_by_paths.return_value = {}

    def _rec(name, ret):
        def _f(*a, **k):
            if order is not None:
                order.append(name)
            return ret

        return _f

    mocks = {
        "acquire_lock": Mock(side_effect=_rec("acquire_lock", True)),
        "release_lock": Mock(side_effect=_rec("release_lock", None)),
        "scan_local": Mock(side_effect=_rec("scan_local", LocalSnapshot())),
        "build_plan": Mock(side_effect=_rec("build_plan", plan)),
        "execute_plan": Mock(
            side_effect=_rec(
                "execute_plan",
                summary if summary is not None else Summary(errors=[]),
            )
        ),
        "Client": Mock(),
        "NotionRemote": Mock(return_value=remote),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(cli, name, mock)
    return mocks, remote


def test_cmd_sync_pipeline_order(tmp_path, monkeypatch):
    """步骤顺序：acquire_lock → scan_local → build_plan → execute_plan → release_lock。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    order = []
    mocks, _ = _patch_pipeline(
        monkeypatch, {"a.md": Classification(Action.PUSH)}, order=order
    )

    cli.cmd_sync(_Args("sync"))

    assert order == [
        "acquire_lock",
        "scan_local",
        "build_plan",
        "execute_plan",
        "release_lock",
    ]
    mocks["execute_plan"].assert_called_once()


def test_cmd_sync_passes_trashed_to_build_plan(tmp_path, monkeypatch):
    """步骤 5 必须显式传第 5 参 remote_paths_trashed（Task C 评审 Important）。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    remote = Mock()
    remote.query_candidates.return_value = [
        {
            "id": "p1",
            "path": "gone.md",
            "last_edited_time": "2026-10-14T10:00:00Z",
            "in_trash": True,
        },
    ]
    captured = {}

    def fake_build_plan(local, remote_index, state, config, trashed=None):
        captured["trashed"] = trashed
        captured["remote_index"] = remote_index
        return {}

    monkeypatch.setattr(cli, "Client", Mock())
    monkeypatch.setattr(cli, "NotionRemote", Mock(return_value=remote))
    monkeypatch.setattr(cli, "acquire_lock", Mock(return_value=True))
    monkeypatch.setattr(cli, "release_lock", Mock())
    monkeypatch.setattr(cli, "scan_local", Mock(return_value=LocalSnapshot()))
    monkeypatch.setattr(cli, "build_plan", fake_build_plan)
    monkeypatch.setattr(cli, "execute_plan", Mock(return_value=Summary(errors=[])))

    cli.cmd_sync(_Args("sync", dry_run=True))

    assert captured["trashed"] == {"gone.md"}
    assert "gone.md" not in captured["remote_index"]
    remote.read_page.assert_not_called()  # 回收站候选不做精判


def test_cmd_sync_dry_run_executes_nothing(tmp_path, monkeypatch):
    """--dry-run 只打印计划，不执行、不推进水位线。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)
    _write_state(
        tmp_path,
        {
            "schema_version": 2,
            "files": {},
            "tombstones": {},
            "notion": {"database_id": "db", "data_source_id": "ds"},
        },
    )

    mocks, _ = _patch_pipeline(monkeypatch, {"a.md": Classification(Action.PUSH)})

    cli.cmd_sync(_Args("sync", dry_run=True))

    mocks["execute_plan"].assert_not_called()
    state = json.loads((tmp_path / ".sync-state.json").read_text())
    assert "remote_watermark" not in state["notion"]


def test_cmd_sync_status_is_read_only(tmp_path, monkeypatch):
    """status 等价于 sync --dry-run：只读，不执行。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    mocks, _ = _patch_pipeline(monkeypatch, {"a.md": Classification(Action.PUSH)})

    cli.cmd_sync(_Args("status"))

    mocks["execute_plan"].assert_not_called()


def test_cmd_sync_releases_lock_on_error(tmp_path, monkeypatch):
    """finally 中释放锁，即使执行抛错。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    mocks, _ = _patch_pipeline(monkeypatch, {"a.md": Classification(Action.PUSH)})
    mocks["execute_plan"].side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cli.cmd_sync(_Args("sync"))

    mocks["release_lock"].assert_called_once()


def test_cmd_sync_exits_when_lock_held(tmp_path, monkeypatch, capsys):
    """锁被占用时退出且不释放他人锁。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    release = Mock()
    monkeypatch.setattr(cli, "acquire_lock", Mock(return_value=False))
    monkeypatch.setattr(cli, "release_lock", release)

    with pytest.raises(SystemExit):
        cli.cmd_sync(_Args("sync"))

    release.assert_not_called()
    assert "另一个同步进程" in capsys.readouterr().out


def test_cmd_sync_advances_watermark_on_success(tmp_path, monkeypatch):
    """无 errors 时推进水位线并 save。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)
    _write_state(
        tmp_path,
        {
            "schema_version": 2,
            "files": {},
            "tombstones": {},
            "notion": {"database_id": "db", "data_source_id": "ds"},
        },
    )

    _patch_pipeline(monkeypatch, {}, summary=Summary(errors=[]))

    cli.cmd_sync(_Args("sync"))

    state = json.loads((tmp_path / ".sync-state.json").read_text())
    assert state["notion"]["remote_watermark"].endswith("Z")


def test_cmd_sync_no_watermark_on_errors(tmp_path, monkeypatch):
    """有 errors 时不推进水位线。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)
    _write_state(
        tmp_path,
        {
            "schema_version": 2,
            "files": {},
            "tombstones": {},
            "notion": {"database_id": "db", "data_source_id": "ds"},
        },
    )

    _patch_pipeline(
        monkeypatch, {}, summary=Summary(errors=[{"path": "a.md", "error": "x"}])
    )

    cli.cmd_sync(_Args("sync"))

    state = json.loads((tmp_path / ".sync-state.json").read_text())
    assert "remote_watermark" not in state["notion"]


def test_cmd_sync_push_warns_on_remote_changes(tmp_path, monkeypatch, capsys):
    """push 计划含 PULL/CONFLICT → 警告，且这些项不执行。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    plan = {
        "push.md": Classification(Action.PUSH),
        "pull.md": Classification(Action.PULL),
        "conflict.md": Classification(Action.CONFLICT),
    }
    mocks, _ = _patch_pipeline(monkeypatch, plan)

    cli.cmd_sync(_Args("push"))

    assert "远端有修改" in capsys.readouterr().out
    executed = mocks["execute_plan"].call_args[0][0]
    assert set(executed) == {"push.md"}


def test_cmd_sync_pull_filters_to_pull_actions(tmp_path, monkeypatch):
    """pull 只执行 PULL/DELETE_LOCAL/ADOPT（+BASELINE）。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    plan = {
        "pull.md": Classification(Action.PULL),
        "delete.md": Classification(Action.DELETE_LOCAL),
        "adopt.md": Classification(Action.ADOPT),
        "create.md": Classification(Action.CREATE_REMOTE),
        "push.md": Classification(Action.PUSH),
        "baseline.md": Classification(Action.BASELINE),
    }
    mocks, _ = _patch_pipeline(monkeypatch, plan)

    cli.cmd_sync(_Args("pull"))

    executed = mocks["execute_plan"].call_args[0][0]
    assert set(executed) == {"pull.md", "delete.md", "adopt.md", "baseline.md"}


def test_cmd_sync_sync_executes_all_executable(tmp_path, monkeypatch):
    """sync 执行全部可执行动作，CONFLICT/SKIP 仍只报告。"""
    from sync_lib import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    plan = {
        "push.md": Classification(Action.PUSH),
        "pull.md": Classification(Action.PULL),
        "create.md": Classification(Action.CREATE_REMOTE),
        "archive.md": Classification(Action.ARCHIVE_REMOTE),
        "delete.md": Classification(Action.DELETE_LOCAL),
        "rename.md": Classification(Action.RENAME),
        "adopt.md": Classification(Action.ADOPT),
        "baseline.md": Classification(Action.BASELINE),
        "conflict.md": Classification(Action.CONFLICT),
        "skip.md": Classification(Action.SKIP),
    }
    mocks, _ = _patch_pipeline(monkeypatch, plan)

    cli.cmd_sync(_Args("sync"))

    executed = mocks["execute_plan"].call_args[0][0]
    assert set(executed) == {
        "push.md",
        "pull.md",
        "create.md",
        "archive.md",
        "delete.md",
        "rename.md",
        "adopt.md",
        "baseline.md",
    }


# --- cli: 动作过滤表（设计 6.1） ---


def _filter_sample():
    return {
        "push.md": Classification(Action.PUSH),
        "pull.md": Classification(Action.PULL),
        "create.md": Classification(Action.CREATE_REMOTE),
        "archive.md": Classification(Action.ARCHIVE_REMOTE),
        "rename.md": Classification(Action.RENAME),
        "adopt.md": Classification(Action.ADOPT),
        "delete.md": Classification(Action.DELETE_LOCAL),
        "conflict.md": Classification(Action.CONFLICT),
        "baseline.md": Classification(Action.BASELINE),
        "skip.md": Classification(Action.SKIP),
    }


def test_filter_plan_push_whitelist():
    from sync_lib import cli

    filtered = cli._filter_plan(_filter_sample(), "push")
    assert set(filtered) == {
        "push.md",
        "create.md",
        "archive.md",
        "rename.md",
        "adopt.md",
        "baseline.md",
    }


def test_filter_plan_pull_whitelist():
    from sync_lib import cli

    filtered = cli._filter_plan(_filter_sample(), "pull")
    assert set(filtered) == {"pull.md", "adopt.md", "delete.md", "baseline.md"}


def test_filter_plan_sync_all_executable():
    from sync_lib import cli

    filtered = cli._filter_plan(_filter_sample(), "sync")
    assert set(filtered) == {
        "push.md",
        "pull.md",
        "create.md",
        "archive.md",
        "rename.md",
        "adopt.md",
        "delete.md",
        "baseline.md",
    }
    # CONFLICT 始终只报告；SKIP 无动作
    assert "conflict.md" not in filtered
    assert "skip.md" not in filtered


# --- cli: 写前本地路径校验（防符号链接逃逸） ---


def test_check_local_containment_rejects_escape(tmp_path):
    from sync_lib import cli

    plan = {
        "ok.md": Classification(Action.PULL),
        "gone.md": Classification(Action.DELETE_LOCAL),
        "push.md": Classification(Action.PUSH),
        "../escape.md": Classification(Action.PULL),
    }
    safe, errors = cli._check_local_containment(plan, tmp_path)

    assert set(safe) == {"ok.md", "gone.md", "push.md"}
    assert len(errors) == 1
    assert errors[0]["path"] == "../escape.md"


# --- cli: remote_index 构建（设计 5.4） ---


def test_build_remote_index_coarse_reads_candidates(tmp_path):
    from sync_lib import cli

    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.state["notion"] = {"remote_watermark": None}
    mgr.update_file(
        "a.md",
        {"page_id": "p1", "base_remote_hash": "BR", "remote_edited_at": "old"},
    )

    remote = Mock()
    remote.query_candidates.return_value = [
        {"id": "p1", "path": "a.md", "last_edited_time": "new", "in_trash": False},
    ]
    remote.read_page.return_value = ("content", "b1", True)

    index, trashed = cli._build_remote_index(remote, mgr, {}, LocalSnapshot(), False)

    assert index["a.md"]["page_id"] == "p1"
    assert index["a.md"]["hash"] == compute_hash("content")
    assert index["a.md"]["struct_valid"] is True
    assert index["a.md"]["last_edited_time"] == "new"
    assert mgr.state["files"]["a.md"]["remote_edited_at"] == "new"
    assert trashed == set()
    # watermark=None → 全量（since_iso=None）
    remote.query_candidates.assert_called_once_with(None)


def test_build_remote_index_non_candidate_uses_br(tmp_path):
    from sync_lib import cli

    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.state["notion"] = {"remote_watermark": "2026-10-14T00:00:00Z"}
    mgr.update_file(
        "b.md",
        {"page_id": "p2", "base_remote_hash": "BR2", "remote_edited_at": "t2"},
    )

    remote = Mock()
    remote.query_candidates.return_value = []

    index, _ = cli._build_remote_index(remote, mgr, {}, LocalSnapshot(), False)

    assert index["b.md"] == {
        "page_id": "p2",
        "hash": "BR2",
        "struct_valid": True,
        "last_edited_time": "t2",
    }


def test_build_remote_index_probes_adopt(tmp_path):
    from sync_lib import cli

    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.state["notion"] = {"remote_watermark": None}

    snapshot = LocalSnapshot(
        files={"new.md": {"hash": "h", "format": {}, "size_kb": 0.0}}
    )
    remote = Mock()
    remote.query_candidates.return_value = []
    remote.query_by_paths.return_value = {"new.md": "p9"}
    remote.read_page.return_value = ("same", "b9", True)

    index, _ = cli._build_remote_index(remote, mgr, {}, snapshot, False)

    assert index["new.md"]["page_id"] == "p9"
    assert index["new.md"]["hash"] == compute_hash("same")
    remote.query_by_paths.assert_called_once_with(["new.md"])


def test_build_remote_index_full_skips_coarse(tmp_path):
    from sync_lib import cli

    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.state["notion"] = {"remote_watermark": "2026-10-14T00:00:00Z"}
    mgr.update_file(
        "a.md",
        {"page_id": "p1", "base_remote_hash": "BR", "remote_edited_at": "t"},
    )

    remote = Mock()
    remote.read_page.return_value = ("content", "b1", True)

    index, _ = cli._build_remote_index(remote, mgr, {}, LocalSnapshot(), True)

    remote.query_candidates.assert_not_called()
    assert index["a.md"]["page_id"] == "p1"
    assert index["a.md"]["hash"] == compute_hash("content")


def test_watermark_since_applies_overlap():
    from sync_lib import cli

    since = cli._watermark_since("2026-10-14T10:00:00Z", {"remote_overlap_minutes": 5})
    assert since == "2026-10-14T09:55:00Z"


def test_watermark_since_none_is_full():
    from sync_lib import cli

    assert cli._watermark_since(None, {}) is None
