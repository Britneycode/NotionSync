"""Tests for the Notion sync canonical module (text normalization, format
detection/restoration, rich_text splitting and path validation)."""

import json
import os

from sync_lib.canonical import (
    canonical,
    detect_format,
    normalize_path,
    restore_format,
    split_rich_text,
    validate_path,
)
from sync_lib.planner import (
    Action,
    classify_existence,
    classify_file,
    detect_renames,
)
from sync_lib.scanner import scan_local
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
