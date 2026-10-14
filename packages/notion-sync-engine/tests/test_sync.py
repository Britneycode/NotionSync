"""Tests for the Notion sync canonical module (text normalization, format
detection/restoration, rich_text splitting and path validation)."""

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
