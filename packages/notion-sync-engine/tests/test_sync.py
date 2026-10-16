"""Tests for the Notion sync canonical module (text normalization, format
detection/restoration, rich_text splitting and path validation)."""

import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from keyring.errors import PasswordDeleteError

from sync_lib.canonical import (
    canonical,
    compute_hash,
    detect_format,
    normalize_path,
    restore_format,
    split_rich_text,
    validate_path,
)
from sync_lib import api, credentials
from sync_lib.api import RunOptions
from sync_lib.cancel import CancelToken, Cancelled
from sync_lib.executor import (
    ResolveResult,
    Summary,
    _br_from_update_response,
    backup_remote_content,
    execute_plan,
    resolve_conflict,
)
from sync_lib.lock import LockBusy, RunLock
from sync_lib.plan import build_plan
from sync_lib.protocol import (
    EXIT,
    PROTOCOL_VERSION,
    plan_fingerprint,
    plan_row,
    side_label,
)
from sync_lib.reporter import ConsoleReporter, JsonlReporter
from sync_lib.planner import (
    Action,
    Classification,
    classify_existence,
    classify_file,
    detect_renames,
)
from sync_lib.remote import (
    MAX_BLOCK_UNITS,
    MAX_RICH_TEXT_ELEMENTS,
    NotionRemote,
    RateLimiter,
    retry_with_backoff,
)
from sync_lib.scanner import LocalSnapshot, scan_local
from sync_lib.state import StateManager, cleanup_tmp_files


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


# --- lock: 内核文件锁 ---
#
# 旧用例断言的是 PID 文件锁（acquire_lock/release_lock）。契约已变：锁改由内核
# 持有（filelock），不再读 PID、不再把残留的 .sync.lock 当回事，所以下面重写。


def test_run_lock_acquires_and_releases(tmp_path):
    with RunLock(tmp_path, "sync"):
        assert (tmp_path / ".sync.lock.json").exists()
    assert not (tmp_path / ".sync.lock.json").exists()


def test_run_lock_busy_reports_holder(tmp_path):
    """同一根目录第二个 RunLock 立刻失败，并带上持有者信息。"""
    with RunLock(tmp_path, "push"):
        with pytest.raises(LockBusy) as ei:
            RunLock(tmp_path, "sync").__enter__()
    assert ei.value.holder["command"] == "push"
    assert ei.value.holder["pid"] == os.getpid()


def test_run_lock_releases_on_error(tmp_path):
    """with 体里抛错也要放锁。"""
    with pytest.raises(RuntimeError):
        with RunLock(tmp_path, "sync"):
            raise RuntimeError("boom")
    with RunLock(tmp_path, "sync"):
        pass


def test_run_lock_ignores_stale_file(tmp_path):
    """磁盘上残留的 .sync.lock 没有语义——内核锁不看它。"""
    (tmp_path / ".sync.lock").write_text('{"pid": 99999}')
    with RunLock(tmp_path, "sync"):
        pass


def _wait_lock_free(root, timeout=10.0):
    """等到能拿到锁为止。

    Windows 上进程被 TerminateProcess 之后，wait() 返回时句柄可能还没全部关闭，
    锁的释放略有延迟，所以这里给一点重试余量。
    """
    deadline = time.time() + timeout
    while True:
        try:
            with RunLock(root, "sync"):
                return
        except LockBusy:
            if time.time() > deadline:
                raise
            time.sleep(0.05)


def test_run_lock_released_when_holder_killed(tmp_path):
    """强杀持锁进程后，锁必须自动释放（旧 PID 锁做不到这点）。"""
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from sync_lib.lock import RunLock\n"
        "with RunLock(Path(sys.argv[1]), 'sync'):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "locked"
        # 进程活着时拿不到锁
        with pytest.raises(LockBusy):
            RunLock(tmp_path, "sync").__enter__()
        proc.kill()
        proc.wait(timeout=10)
        # 被强杀（没有走 __exit__）后，内核会释放锁
        _wait_lock_free(tmp_path)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


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


def test_scanner_builtin_excludes_sync_dot_files(tmp_path):
    """内置排除含 .sync.*：锁文件不被扫描（include 放宽到 ** 也不自激）。"""
    (tmp_path / ".sync.lock").write_text("pid")
    (tmp_path / ".sync.lock.json").write_text("{}")
    (tmp_path / "a.md").write_text("content")

    snapshot = scan_local(tmp_path, {"include": ["**"], "exclude": []})

    assert set(snapshot.files) == {"a.md"}


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


def test_scanner_accepts_file_over_old_150k_limit(tmp_path):
    """超过旧 15 万上限但在新上限（默认 100 万）内的文本不再被整文件拒绝。

    块级拆分能把它装进一个 page 的多个 code block，所以 scanner 不该拦。
    """
    large_content = "a" * 200_000  # 20 万码元，超旧 150_000、低于新默认 1_000_000
    (tmp_path / "big.md").write_text(large_content)
    config = {"include": ["*.md"], "exclude": []}  # 不显式给 max_chars，走新默认
    snapshot = scan_local(tmp_path, config)

    assert "big.md" in snapshot
    assert snapshot.skipped == []


def test_oversized_text_roundtrip_via_multiple_code_blocks(tmp_path):
    """端到端：超 15 万的文本经 create_page 拆块、read_page 拼接，内容无损。"""
    content = "中文🙂" * (MAX_BLOCK_UNITS // 4 + 1)  # 多块（中文+代理对，压边界）
    assert len(content.encode("utf-16-le")) // 2 > 150_000  # 确实超旧上限

    mock_client = Mock()
    mock_client.pages.create.return_value = {"id": "page123"}
    remote = NotionRemote(mock_client, "db123", "ds123")
    remote.create_page("big.md", content, "markdown", "", len(content))

    children = mock_client.pages.create.call_args[1]["children"]
    assert len(children) > 1  # 拆成了多块
    blocks = [
        {
            "id": f"block{i}",
            "type": "code",
            "code": {
                "language": c["code"]["language"],
                "rich_text": [{"plain_text": rt["text"]["content"]} for rt in c["code"]["rich_text"]],
            },
        }
        for i, c in enumerate(children)
    ]
    mock_client.blocks.children.list.return_value = {"results": blocks, "has_more": False}

    read_back, _, struct_valid = remote.read_page("page123")

    assert struct_valid is True
    assert read_back == content
    assert compute_hash(read_back) == compute_hash(content)


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
    # 未超单块容量时仍只建一个 code block
    assert len(call_args["children"]) == 1
    assert call_args["children"][0]["code"]["rich_text"][0]["text"]["content"] == "# Hello"


def test_create_page_splits_oversized_content_into_multiple_code_blocks():
    """正文超过单块容量时拆成多个 code block，每块 rich_text ≤100 段。"""
    mock_client = Mock()
    mock_client.pages.create.return_value = {"id": "page123"}

    remote = NotionRemote(mock_client, "db123", "ds123")
    content = "a" * (MAX_BLOCK_UNITS + 1)
    remote.create_page(
        path="big.md", content=content, file_type="markdown", directory="", chars=len(content),
    )

    children = mock_client.pages.create.call_args[1]["children"]
    assert len(children) == 2
    assert all(c["type"] == "code" for c in children)
    assert len(children[0]["code"]["rich_text"]) == MAX_RICH_TEXT_ELEMENTS
    assert len(children[1]["code"]["rich_text"]) == 1
    joined = "".join(
        rt["text"]["content"] for c in children for rt in c["code"]["rich_text"]
    )
    assert joined == content


def test_create_page_splits_without_breaking_surrogate_pairs():
    """拆分点落在非 BMP 字符上时不得切碎代理对，且顺序拼接可无损还原。"""
    mock_client = Mock()
    mock_client.pages.create.return_value = {"id": "page123"}

    remote = NotionRemote(mock_client, "db123", "ds123")
    content = "中文🙂" * (MAX_BLOCK_UNITS // 4 + 1)
    remote.create_page(
        path="big.md", content=content, file_type="markdown", directory="", chars=0,
    )

    children = mock_client.pages.create.call_args[1]["children"]
    assert len(children) == 2
    joined = "".join(
        rt["text"]["content"] for c in children for rt in c["code"]["rich_text"]
    )
    assert joined == content


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


def test_read_page_concatenates_multiple_code_blocks():
    """多块页面按文档顺序拼接还原（超长正文拆分后的读回路径）。"""
    mock_client = Mock()
    mock_client.blocks.children.list.return_value = {
        "results": [
            {"id": "block1", "type": "code",
             "code": {"rich_text": [{"plain_text": "line1\n"}, {"plain_text": "line2"}]}},
            {"id": "block2", "type": "code",
             "code": {"rich_text": [{"plain_text": "line3"}]}},
        ],
        "has_more": False,
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    content, block_id, struct_valid = remote.read_page("page123")

    assert content == "line1\nline2line3"
    assert block_id == "block1"
    assert struct_valid is True


def test_read_page_invalid_structure_non_code_block():
    mock_client = Mock()
    mock_client.blocks.children.list.return_value = {
        "results": [{"id": "b1", "type": "paragraph", "paragraph": {"rich_text": []}}],
        "has_more": False,
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    content, block_id, struct_valid = remote.read_page("page123")

    assert struct_valid is False


def test_read_page_invalid_structure_empty_page():
    mock_client = Mock()
    mock_client.blocks.children.list.return_value = {"results": [], "has_more": False}

    remote = NotionRemote(mock_client, "db123", "ds123")
    content, block_id, struct_valid = remote.read_page("page123")

    assert struct_valid is False


def test_create_then_read_roundtrip_for_oversized_content():
    """create_page 拆出的多块，经 read_page 必须无损拼回（否则 PULL 会损坏内容）。"""
    mock_client = Mock()
    mock_client.pages.create.return_value = {"id": "page123"}

    remote = NotionRemote(mock_client, "db123", "ds123")
    content = "中文🙂" * (MAX_BLOCK_UNITS // 4 + 1)
    remote.create_page("big.md", content, "markdown", "", 0)

    # 把建页时的 children 转成 blocks.children.list 的响应形状：
    # 真实 API 会在 rich_text 每项上附 plain_text，并给每个块分配 id
    children = mock_client.pages.create.call_args[1]["children"]
    blocks = [
        {
            "id": f"block{index}",
            "type": "code",
            "code": {
                "language": child["code"]["language"],
                "rich_text": [
                    {"plain_text": rt["text"]["content"]} for rt in child["code"]["rich_text"]
                ],
            },
        }
        for index, child in enumerate(children)
    ]
    mock_client.blocks.children.list.return_value = {"results": blocks, "has_more": False}

    read_back, block_id, struct_valid = remote.read_page("page123")

    assert struct_valid is True
    assert block_id == "block0"
    assert read_back == content


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


# --- remote: update_page_content（整页替换，必要时增删 code block） ---


def _code_block_stub(block_id, text, language="markdown"):
    return {
        "id": block_id,
        "type": "code",
        "code": {"rich_text": [{"plain_text": text}], "language": language},
    }


def test_update_page_content_single_block_returns_write_response():
    """单块整页：就地替换并返回写响应体，BR 仍可由响应体算出。"""
    mock_client = Mock()
    mock_client.blocks.children.list.return_value = {
        "results": [_code_block_stub("b1", "old")],
        "has_more": False,
    }
    mock_client.blocks.update.return_value = {
        "id": "b1",
        "type": "code",
        "code": {"rich_text": [{"plain_text": "new"}]},
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    resp = remote.update_page_content("p1", "new")

    assert resp["id"] == "b1"
    assert mock_client.blocks.update.call_args[1]["block_id"] == "b1"
    assert mock_client.blocks.children.append.call_count == 0
    assert mock_client.blocks.delete.call_count == 0


def test_update_page_content_appends_when_content_grows():
    """正文变长：原地更新首块并在末尾追加新块，返回 None 交调用方读回重算 BR。"""
    mock_client = Mock()
    mock_client.blocks.children.list.return_value = {
        "results": [_code_block_stub("b1", "old")],
        "has_more": False,
    }
    mock_client.blocks.update.return_value = {
        "id": "b1",
        "type": "code",
        "code": {"rich_text": [{"plain_text": "x"}]},
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    resp = remote.update_page_content("p1", "a" * (MAX_BLOCK_UNITS + 1))

    assert resp is None
    assert mock_client.blocks.update.call_args[1]["block_id"] == "b1"
    assert mock_client.blocks.children.append.call_count == 1
    appended = mock_client.blocks.children.append.call_args[1]
    assert appended["block_id"] == "p1"
    assert appended["children"][0]["code"]["language"] == "markdown"
    assert mock_client.blocks.delete.call_count == 0


def test_update_page_content_deletes_surplus_blocks():
    """正文变短：原地更新首块，删除多余的 code block。"""
    mock_client = Mock()
    mock_client.blocks.children.list.return_value = {
        "results": [_code_block_stub("b1", "a"), _code_block_stub("b2", "b")],
        "has_more": False,
    }
    mock_client.blocks.update.return_value = {
        "id": "b1",
        "type": "code",
        "code": {"rich_text": [{"plain_text": "short"}]},
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    resp = remote.update_page_content("p1", "short")

    assert resp is None
    assert mock_client.blocks.delete.call_count == 1
    assert mock_client.blocks.delete.call_args[1]["block_id"] == "b2"
    assert mock_client.blocks.children.append.call_count == 0


def test_update_page_content_updates_every_block_in_place():
    """块数不变（多块）：逐块就地更新，首块 id 保持稳定。"""
    mock_client = Mock()
    mock_client.blocks.children.list.return_value = {
        "results": [_code_block_stub("b1", "a"), _code_block_stub("b2", "b")],
        "has_more": False,
    }
    mock_client.blocks.update.return_value = {
        "id": "b1",
        "type": "code",
        "code": {"rich_text": [{"plain_text": "x"}]},
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    resp = remote.update_page_content("p1", "a" * (MAX_BLOCK_UNITS + 1))

    assert resp is None
    updated = [call[1]["block_id"] for call in mock_client.blocks.update.call_args_list]
    assert updated == ["b1", "b2"]
    assert mock_client.blocks.children.append.call_count == 0
    assert mock_client.blocks.delete.call_count == 0


def test_update_page_content_non_code_block_returns_none():
    """结构异常：不写入，返回 None 由调用方按读回结果处理。"""
    mock_client = Mock()
    mock_client.blocks.children.list.return_value = {
        "results": [{"id": "b1", "type": "paragraph", "paragraph": {"rich_text": []}}],
        "has_more": False,
    }

    remote = NotionRemote(mock_client, "db123", "ds123")
    resp = remote.update_page_content("p1", "new")

    assert resp is None
    assert mock_client.blocks.update.call_count == 0


# --- executor: 执行计划（PUSH/PULL + 乐观并发校验） ---


def test_execute_push(tmp_path):
    # 准备
    (tmp_path / "test.md").write_text("new content")

    state_mgr = Mock()
    remote = Mock()
    remote.read_page.return_value = ("old content", "block123", True)
    remote.update_page_content.return_value = {"last_edited_time": "2026-10-14T12:00:00Z"}

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
    remote.update_page_content.assert_called_once()
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


# --- remote: search_pages（界面「选择目标页面」的只读查询） ---


def _search_page(page_id, title, url="https://www.notion.so/x", prop="title"):
    """构造 search 返回的 page 对象：标题在 properties 里那个 title 型属性上。"""
    return {
        "object": "page",
        "id": page_id,
        "url": url,
        "properties": {
            prop: {"id": prop, "type": "title", "title": [{"plain_text": title}]},
        },
    }


def test_search_pages_reads_title_and_url():
    mock_client = Mock()
    mock_client.search.return_value = {
        "results": [_search_page("p1", "工作台", "https://www.notion.so/p1")],
        "has_more": False,
    }

    remote = NotionRemote(mock_client, None, None)
    remote.limiter = Mock()

    assert remote.search_pages() == [
        {"id": "p1", "title": "工作台", "url": "https://www.notion.so/p1"}
    ]
    remote.limiter.wait.assert_called_once()
    body = mock_client.search.call_args[1]
    assert body["filter"] == {"property": "object", "value": "page"}
    assert "start_cursor" not in body  # 首页不带游标


def test_search_pages_paginates_and_sorts_by_title():
    mock_client = Mock()
    mock_client.search.side_effect = [
        {"results": [_search_page("p1", "B 页")], "has_more": True, "next_cursor": "c1"},
        {"results": [_search_page("p2", "A 页")], "has_more": False},
    ]

    remote = NotionRemote(mock_client, None, None)
    remote.limiter = Mock()

    pages = remote.search_pages()

    assert [p["id"] for p in pages] == ["p2", "p1"]  # 按标题排，不是按返回顺序
    assert mock_client.search.call_count == 2
    assert mock_client.search.call_args_list[1][1]["start_cursor"] == "c1"


def test_search_pages_takes_the_title_property_from_a_database_row():
    """挂在数据库下的行，标题属性叫 Name 而不是 title。"""
    mock_client = Mock()
    mock_client.search.return_value = {
        "results": [
            {
                "id": "p1",
                "url": "u",
                "properties": {
                    "Path": {"type": "rich_text", "rich_text": [{"plain_text": "a.md"}]},
                    "Name": {"type": "title", "title": [{"plain_text": "a.md"}]},
                },
            }
        ],
        "has_more": False,
    }

    remote = NotionRemote(mock_client, None, None)
    remote.limiter = Mock()

    assert remote.search_pages()[0]["title"] == "a.md"


def test_search_pages_without_a_title_property_is_empty_not_a_crash():
    mock_client = Mock()
    mock_client.search.return_value = {
        "results": [{"id": "p1", "properties": {}}],
        "has_more": False,
    }

    remote = NotionRemote(mock_client, None, None)
    remote.limiter = Mock()

    assert remote.search_pages() == [{"id": "p1", "title": "", "url": ""}]


def test_search_pages_with_nothing_visible_returns_empty():
    """integration 没被连接到任何页面时，search 就是空的——不是错误。"""
    mock_client = Mock()
    mock_client.search.return_value = {"results": [], "has_more": False}

    remote = NotionRemote(mock_client, None, None)
    remote.limiter = Mock()

    assert remote.search_pages() == []


# --- cli: 命令入口（Task 8） ---


def test_cli_init(tmp_path, monkeypatch):
    from sync_lib.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "test_token")

    with patch("sync_lib.api.NotionRemote") as mock_remote:
        mock_remote.return_value.create_database.return_value = ("db123", "ds123")

        with patch("sys.argv", ["sync.py", "init", "--parent-page-id", "page123"]):
            main()

        # 验证配置文件生成
        assert (tmp_path / ".sync-config.yaml").exists()
        assert (tmp_path / ".sync-state.json").exists()


def test_cli_only_file_is_read_into_options(tmp_path, monkeypatch):
    """--only-file：每行一个路径，空行忽略，反斜杠归一成与计划行一致的 POSIX 形式。"""
    from sync_lib import cli

    listing = tmp_path / "only.txt"
    listing.write_text("a.md\n\ndocs\\b.md\n  \n", encoding="utf-8")
    seen = {}

    def fake_run(command, root, opts, rep, cancel):
        seen["opts"] = opts
        return api.RunResult(0, {})

    monkeypatch.setattr(cli, "run", fake_run)
    code = cli.main(["sync", "--root", str(tmp_path), "--only-file", str(listing)])

    assert code == 0
    assert seen["opts"].only == frozenset({"a.md", "docs/b.md"})


def test_cli_only_file_missing_is_a_usage_error(tmp_path):
    from sync_lib import cli

    with pytest.raises(SystemExit) as e:
        cli.main(["sync", "--only-file", str(tmp_path / "nope.txt")])
    assert e.value.code == 2


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

    with patch("sync_lib.remote.retry_with_backoff", side_effect=lambda f, **kw: f()) as mock_retry:
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
    remote.update_page_content.return_value = {
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
    remote.update_page_content.return_value = {
        "id": "b1",
        "type": "code",
        "code": {"rich_text": [{"text": {"content": "new content"}}]},  # 无 plain_text
    }

    plan = {"test.md": Classification(Action.PUSH)}
    summary = execute_plan(plan, mgr, remote, tmp_path, {})

    assert summary.pushed == 1
    assert mgr.get_file("test.md")["base_remote_hash"] == compute_hash("new content")


def test_execute_push_valid_false_records_error(tmp_path):
    """H-I-2：写前校验结构异常时不崩溃，记入 errors，且不写入远端。"""
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
    remote.update_page_content.assert_not_called()


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
    remote.update_page_content.assert_not_called()


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
    """一次运行的参数命名空间（对齐 api.RunOptions）。"""

    def __init__(
        self, command, dry_run=False, full=False, path=None, keep=None, backup_only=False
    ):
        self.command = command
        self.dry_run = dry_run
        self.full = full
        self.path = path
        self.keep = keep
        self.backup_only = backup_only


def _sync(args, root=None, reporter=None):
    """按旧 cmd_sync 的用法调用 api.run，返回 RunResult。

    编排已从 cli.cmd_sync 搬到 api.run（设计 §7.4），所以这里也换入口。
    """
    from sync_lib.api import RunOptions, run
    from sync_lib.cancel import CancelToken
    from sync_lib.reporter import ConsoleReporter

    opts = RunOptions(
        dry_run=args.dry_run,
        full=args.full,
        path=args.path,
        keep=args.keep,
        backup_only=args.backup_only,
    )
    return run(
        args.command,
        root or Path.cwd(),
        opts,
        reporter if reporter is not None else ConsoleReporter(sys.stdout),
        CancelToken(),
    )


class _FakeLock:
    """替代 RunLock，只记录加锁/解锁时机。"""

    def __init__(self, root, command, order=None):
        self._order = order

    def __enter__(self):
        if self._order is not None:
            self._order.append("acquire_lock")
        return self

    def __exit__(self, *exc):
        if self._order is not None:
            self._order.append("release_lock")
        return False


def _write_config(root):
    """写配置 + 一份「已初始化」的状态。

    api 会校验 data_source_id（旧 cmd_sync 不校验，缺了就拿 None 去请求），
    所以编排用例必须给出初始化后的状态。
    """
    (root / ".sync-config.yaml").write_text(
        "include: ['*.md']\nexclude: []\nremote_overlap_minutes: 5\n"
    )
    _write_state(
        root,
        {
            "schema_version": 2,
            "files": {},
            "tombstones": {},
            "notion": {"database_id": "db", "data_source_id": "ds"},
        },
    )


def _write_state(root, state):
    (root / ".sync-state.json").write_text(json.dumps(state))


def _executed(mocks):
    """api.run 逐项调用 execute_plan，把所有调用里的路径并起来。

    逐项调用是为了在文件边界留取消检查点，同时让 executor.py 一行不改；
    语义与旧的一次性批量调用等价（execute_plan 本来就是逐项 try/except）。
    """
    paths = set()
    for call in mocks["execute_plan"].call_args_list:
        paths |= set(call[0][0])
    return paths


def _patch_pipeline(monkeypatch, plan, summary=None, order=None, remote=None):
    """把 api.run 的外部依赖替换为可控 mock，返回 (mocks, remote)。"""
    from sync_lib import api

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
        "RunLock": lambda root, command: _FakeLock(root, command, order),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(api, name, mock)
    return mocks, remote


def test_cmd_sync_pipeline_order(tmp_path, monkeypatch):
    """步骤顺序：acquire_lock → scan_local → build_plan → execute_plan → release_lock。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    order = []
    mocks, _ = _patch_pipeline(
        monkeypatch, {"a.md": Classification(Action.PUSH)}, order=order
    )

    _sync(_Args("sync"))

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

    monkeypatch.setattr(api, "Client", Mock())
    monkeypatch.setattr(api, "NotionRemote", Mock(return_value=remote))
    monkeypatch.setattr(api, "RunLock", lambda root, command: _FakeLock(root, command))
    monkeypatch.setattr(api, "scan_local", Mock(return_value=LocalSnapshot()))
    monkeypatch.setattr(api, "build_plan", fake_build_plan)
    monkeypatch.setattr(api, "execute_plan", Mock(return_value=Summary(errors=[])))

    _sync(_Args("sync", dry_run=True))

    assert captured["trashed"] == {"gone.md"}
    assert "gone.md" not in captured["remote_index"]
    remote.read_page.assert_not_called()  # 回收站候选不做精判


def test_cmd_sync_dry_run_executes_nothing(tmp_path, monkeypatch):
    """--dry-run 只打印计划，不执行、不推进水位线。"""
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

    _sync(_Args("sync", dry_run=True))

    mocks["execute_plan"].assert_not_called()
    state = json.loads((tmp_path / ".sync-state.json").read_text())
    assert "remote_watermark" not in state["notion"]


def test_cmd_sync_status_is_read_only(tmp_path, monkeypatch):
    """status 等价于 sync --dry-run：只读，不执行。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    mocks, _ = _patch_pipeline(monkeypatch, {"a.md": Classification(Action.PUSH)})

    _sync(_Args("status"))

    mocks["execute_plan"].assert_not_called()


def test_cmd_sync_releases_lock_on_error(tmp_path, monkeypatch):
    """执行抛错时 with 也要退出并放锁。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    order = []
    mocks, _ = _patch_pipeline(
        monkeypatch, {"a.md": Classification(Action.PUSH)}, order=order
    )
    mocks["execute_plan"].side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _sync(_Args("sync"))

    assert order[-1] == "release_lock"


def test_cmd_sync_reports_lock_busy(tmp_path, monkeypatch, capsys):
    """锁被占用：返回 LOCK_BUSY，文本与旧 CLI 一致，且不去碰别人的锁。"""
    from sync_lib.protocol import EXIT

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    # 真锁真的被占用（另一个 RunLock 正持着）
    holder = RunLock(tmp_path, "push")
    holder.__enter__()
    try:
        result = _sync(_Args("sync"))
    finally:
        holder.__exit__()

    assert result.exit_code == EXIT.LOCK_BUSY
    assert "另一个同步进程" in capsys.readouterr().out


def test_cmd_sync_advances_watermark_on_success(tmp_path, monkeypatch):
    """无 errors 时推进水位线并 save。"""
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

    _sync(_Args("sync"))

    state = json.loads((tmp_path / ".sync-state.json").read_text())
    assert state["notion"]["remote_watermark"].endswith("Z")


def test_cmd_sync_no_watermark_on_errors(tmp_path, monkeypatch):
    """有 errors 时不推进水位线。"""
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

    # 计划里必须真有一条，execute_plan 才会被调用并返回这份带 errors 的摘要
    _patch_pipeline(
        monkeypatch,
        {"a.md": Classification(Action.PUSH)},
        summary=Summary(errors=[{"path": "a.md", "error": "x"}]),
    )

    _sync(_Args("sync"))

    state = json.loads((tmp_path / ".sync-state.json").read_text())
    assert "remote_watermark" not in state["notion"]


def _write_dirty_config(tmp_path):
    """配置 + 两个已跟踪文件，用于 B2 的 run 层用例。"""
    _write_config(tmp_path)
    _write_state(
        tmp_path,
        {
            "schema_version": 2,
            "files": {
                "a.md": {"page_id": "p1", "base_remote_hash": "BR_A"},
                "b.md": {"page_id": "p2", "base_remote_hash": "BR_B"},
            },
            "tombstones": {},
            "notion": {"database_id": "db", "data_source_id": "ds"},
        },
    )


def _dirty_remote(content="changed"):
    remote = Mock()
    remote.query_candidates.return_value = [
        {"id": "p2", "path": "b.md", "last_edited_time": "new", "in_trash": False},
    ]
    remote.read_page.return_value = (content, "b1", True)
    remote.query_by_paths.return_value = {}
    return remote


def test_run_records_unabsorbed_remote_change_in_dirty_set(tmp_path, monkeypatch):
    """远端有变更但本轮未吸收（勾选子集 / push）→ 记入 remote_dirty，而非漏检。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_dirty_config(tmp_path)

    # 过滤后的计划里只有 a.md：b.md 的 PULL 本轮没被执行
    _patch_pipeline(
        monkeypatch, {"a.md": Classification(Action.PUSH)}, remote=_dirty_remote()
    )

    result = _sync(_Args("sync"))

    assert result.exit_code == EXIT.OK
    state = json.loads((tmp_path / ".sync-state.json").read_text())
    assert state["notion"]["remote_dirty"] == ["b.md"]
    assert state["notion"]["remote_watermark"].endswith("Z")


def test_run_clears_dirty_set_when_remote_matches_baseline(tmp_path, monkeypatch):
    """远端与基线一致（无未吸收变更）→ remote_dirty 为空列表。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)
    _write_state(
        tmp_path,
        {
            "schema_version": 2,
            "files": {"a.md": {"page_id": "p1", "base_remote_hash": compute_hash("same")}},
            "tombstones": {},
            "notion": {"database_id": "db", "data_source_id": "ds"},
        },
    )
    remote = Mock()
    remote.query_candidates.return_value = [
        {"id": "p1", "path": "a.md", "last_edited_time": "t", "in_trash": False},
    ]
    remote.read_page.return_value = ("same", "b1", True)
    remote.query_by_paths.return_value = {}
    _patch_pipeline(monkeypatch, {}, remote=remote)

    _sync(_Args("sync"))

    state = json.loads((tmp_path / ".sync-state.json").read_text())
    assert state["notion"]["remote_dirty"] == []


def test_run_dirty_paths_are_rechecked_after_watermark_advance(tmp_path, monkeypatch):
    """水位线推进后，dirty 路径仍被强制读回：下一轮照样发现远端变更（B2 不漏检）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_dirty_config(tmp_path)

    # 第一轮：b.md 是候选且已变更，但计划里没它（未吸收）
    remote = _dirty_remote()
    _patch_pipeline(monkeypatch, {"a.md": Classification(Action.PUSH)}, remote=remote)
    _sync(_Args("sync"))

    # 第二轮：水位线已推进，粗筛不再返回 b.md
    remote.query_candidates.return_value = []
    mocks, _ = _patch_pipeline(
        monkeypatch, {"b.md": Classification(Action.PULL)}, remote=remote
    )
    _sync(_Args("sync"))

    index = mocks["build_plan"].call_args[0][1]
    assert index["b.md"]["hash"] == compute_hash("changed")


def test_run_cancel_leaves_dirty_set_and_watermark_untouched(tmp_path, monkeypatch):
    """取消：水位线不推进，dirty 集合原样保留。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_dirty_config(tmp_path)
    state = json.loads((tmp_path / ".sync-state.json").read_text())
    state["notion"]["remote_watermark"] = "2026-10-14T00:00:00Z"
    state["notion"]["remote_dirty"] = ["b.md"]
    _write_state(tmp_path, state)

    token = CancelToken()

    def fake_execute(item, *a, **k):
        token.cancel()  # 第一个文件跑完就取消
        return Summary(errors=[])

    from sync_lib import api as api_mod

    _patch_pipeline(
        monkeypatch,
        {"a.md": Classification(Action.PUSH), "b.md": Classification(Action.PUSH)},
        remote=_dirty_remote(),
    )
    monkeypatch.setattr(api_mod, "execute_plan", fake_execute)

    result = api_mod.run("sync", tmp_path, RunOptions(), ConsoleReporter(sys.stdout), token)

    assert result.exit_code == EXIT.CANCELLED
    state = json.loads((tmp_path / ".sync-state.json").read_text())
    assert state["notion"]["remote_watermark"] == "2026-10-14T00:00:00Z"
    assert state["notion"]["remote_dirty"] == ["b.md"]


def test_cmd_sync_push_warns_on_remote_changes(tmp_path, monkeypatch, capsys):
    """push 计划含 PULL/CONFLICT → 警告，且这些项不执行。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    plan = {
        "push.md": Classification(Action.PUSH),
        "pull.md": Classification(Action.PULL),
        "conflict.md": Classification(Action.CONFLICT),
    }
    mocks, _ = _patch_pipeline(monkeypatch, plan)

    _sync(_Args("push"))

    assert "远端有修改" in capsys.readouterr().out
    executed = _executed(mocks)
    assert set(executed) == {"push.md"}


def test_cmd_sync_pull_filters_to_pull_actions(tmp_path, monkeypatch):
    """pull 只执行 PULL/DELETE_LOCAL/ADOPT（+BASELINE）。"""
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

    _sync(_Args("pull"))

    executed = _executed(mocks)
    assert set(executed) == {"pull.md", "delete.md", "adopt.md", "baseline.md"}


def test_cmd_sync_sync_executes_all_executable(tmp_path, monkeypatch):
    """sync 执行全部可执行动作，CONFLICT/SKIP 仍只报告。"""
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

    _sync(_Args("sync"))

    executed = _executed(mocks)
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
    filtered = api._filter_plan(_filter_sample(), "push")
    assert set(filtered) == {
        "push.md",
        "create.md",
        "archive.md",
        "rename.md",
        "adopt.md",
        "baseline.md",
    }


def test_filter_plan_pull_whitelist():
    filtered = api._filter_plan(_filter_sample(), "pull")
    assert set(filtered) == {"pull.md", "adopt.md", "delete.md", "baseline.md"}


def test_filter_plan_sync_all_executable():
    filtered = api._filter_plan(_filter_sample(), "sync")
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
    plan = {
        "ok.md": Classification(Action.PULL),
        "gone.md": Classification(Action.DELETE_LOCAL),
        "push.md": Classification(Action.PUSH),
        "../escape.md": Classification(Action.PULL),
    }
    safe, errors = api._check_local_containment(plan, tmp_path)

    assert set(safe) == {"ok.md", "gone.md", "push.md"}
    assert len(errors) == 1
    assert errors[0]["path"] == "../escape.md"


# --- cli: remote_index 构建（设计 5.4） ---


def test_build_remote_index_coarse_reads_candidates(tmp_path):
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

    index, trashed = api._build_remote_index(remote, mgr, {}, LocalSnapshot(), False)

    assert index["a.md"]["page_id"] == "p1"
    assert index["a.md"]["hash"] == compute_hash("content")
    assert index["a.md"]["struct_valid"] is True
    assert index["a.md"]["last_edited_time"] == "new"
    assert mgr.state["files"]["a.md"]["remote_edited_at"] == "new"
    assert trashed == set()
    # watermark=None → 全量（since_iso=None）
    remote.query_candidates.assert_called_once_with(None)


def test_build_remote_index_non_candidate_uses_br(tmp_path):
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.state["notion"] = {"remote_watermark": "2026-10-14T00:00:00Z"}
    mgr.update_file(
        "b.md",
        {"page_id": "p2", "base_remote_hash": "BR2", "remote_edited_at": "t2"},
    )

    remote = Mock()
    remote.query_candidates.return_value = []

    index, _ = api._build_remote_index(remote, mgr, {}, LocalSnapshot(), False)

    assert index["b.md"] == {
        "page_id": "p2",
        "hash": "BR2",
        "struct_valid": True,
        "last_edited_time": "t2",
    }


def test_build_remote_index_reads_dirty_paths_from_state(tmp_path):
    """水位线推进后，remote_dirty 里的页面仍被强制读回（B2）。"""
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.state["notion"] = {
        "remote_watermark": "2026-10-14T00:00:00Z",
        "remote_dirty": ["b.md"],
    }
    mgr.update_file(
        "b.md",
        {"page_id": "p2", "base_remote_hash": "BR2", "remote_edited_at": "t2"},
    )

    remote = Mock()
    remote.query_candidates.return_value = []  # 粗筛因水位线推进漏掉它
    remote.read_page.return_value = ("changed", "b2", True)

    index, _ = api._build_remote_index(remote, mgr, {}, LocalSnapshot(), False)

    remote.read_page.assert_called_once_with("p2")
    assert index["b.md"]["page_id"] == "p2"
    assert index["b.md"]["hash"] == compute_hash("changed")
    assert index["b.md"].get("fresh") is True


def test_build_remote_index_marks_read_entries_fresh(tmp_path):
    """本轮真实读回的条目标 fresh；「视为 R == BR」的合成条目不标。"""
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.state["notion"] = {"remote_watermark": "2026-10-14T00:00:00Z"}
    mgr.update_file("a.md", {"page_id": "p1", "base_remote_hash": "BR_A"})
    mgr.update_file("b.md", {"page_id": "p2", "base_remote_hash": "BR_B"})

    remote = Mock()
    remote.query_candidates.return_value = [
        {"id": "p1", "path": "a.md", "last_edited_time": "t", "in_trash": False},
    ]
    remote.read_page.return_value = ("content", "b1", True)

    index, _ = api._build_remote_index(remote, mgr, {}, LocalSnapshot(), False)

    assert index["a.md"].get("fresh") is True
    assert not index["b.md"].get("fresh")


def test_build_remote_index_probes_adopt(tmp_path):
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

    index, _ = api._build_remote_index(remote, mgr, {}, snapshot, False)

    assert index["new.md"]["page_id"] == "p9"
    assert index["new.md"]["hash"] == compute_hash("same")
    remote.query_by_paths.assert_called_once_with(["new.md"])


def test_build_remote_index_full_skips_coarse(tmp_path):
    mgr = StateManager(tmp_path)
    mgr.load()
    mgr.state["notion"] = {"remote_watermark": "2026-10-14T00:00:00Z"}
    mgr.update_file(
        "a.md",
        {"page_id": "p1", "base_remote_hash": "BR", "remote_edited_at": "t"},
    )

    remote = Mock()
    remote.read_page.return_value = ("content", "b1", True)

    index, _ = api._build_remote_index(remote, mgr, {}, LocalSnapshot(), True)

    remote.query_candidates.assert_not_called()
    assert index["a.md"]["page_id"] == "p1"
    assert index["a.md"]["hash"] == compute_hash("content")


def test_watermark_since_applies_overlap():
    since = api._watermark_since("2026-10-14T10:00:00Z", {"remote_overlap_minutes": 5})
    assert since == "2026-10-14T09:55:00Z"


def test_watermark_since_none_is_full():
    assert api._watermark_since(None, {}) is None


# ============================================================================
# P1：协议序列化 / 取消 / 文本输出黄金对照（设计 §9 P1 退出条件）
# ============================================================================


def _parse_events(text):
    """把 JSONL 文本解析成事件列表，顺带断言每行都是合法 JSON。"""
    events = []
    for line in text.splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def test_jsonl_reporter_envelope_and_monotonic_seq():
    buf = io.StringIO()
    rep = JsonlReporter(buf, run_id="r-abc123")
    rep.emit("hello", engine_version="1.3.0")
    rep.emit("plan", rows=[], counts={})
    rep.emit("run.finished", exit_code=0, summary={})

    events = _parse_events(buf.getvalue())

    assert [e["seq"] for e in events] == [1, 2, 3]  # 单调递增
    assert {e["v"] for e in events} == {PROTOCOL_VERSION}
    assert {e["run"] for e in events} == {"r-abc123"}
    assert [e["type"] for e in events] == ["hello", "plan", "run.finished"]
    assert events[0]["data"]["engine_version"] == "1.3.0"
    # ts 是带时区的 ISO 8601
    datetime.fromisoformat(events[0]["ts"])


def test_jsonl_reporter_keeps_chinese_readable():
    """ensure_ascii=False：中文按原样输出，便于人肉排查协议流。"""
    buf = io.StringIO()
    JsonlReporter(buf, run_id="r-1").emit("log", message="发现 2 个未完成操作")
    assert "发现 2 个未完成操作" in buf.getvalue()


def test_cli_json_mode_stdout_is_pure_jsonl(tmp_path):
    """真实子进程：--json 模式下 stdout 只有合法 JSONL，且以 run.finished 收尾。"""
    # 空目录 → api 在读配置时就抛 SyncError，不会碰网络
    proc = subprocess.run(
        [sys.executable, "-m", "sync_lib.cli", "status", "--json", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    events = _parse_events(proc.stdout)
    assert events, f"stdout 没有事件：{proc.stdout!r} / {proc.stderr!r}"
    assert events[0]["type"] == "hello"
    assert events[-1]["type"] == "run.finished"
    assert events[-1]["data"]["exit_code"] == EXIT.FATAL
    # 「没有配置」这件事走 warning 事件，而不是污染协议流的裸 print
    warning = [e for e in events if e["type"] == "warning"]
    assert warning and warning[0]["data"]["code"] == "no-config"
    assert "未找到" in warning[0]["data"]["message"]


def test_plan_row_shape_and_fingerprint_stability():
    plan = {
        "b.md": Classification(Action.PUSH, "local newer"),
        "a.md": Classification(Action.PULL),
        "skip.md": Classification(Action.SKIP),
    }
    row = plan_row("b.md", plan["b.md"], "changed", "same")
    assert row == {
        "path": "b.md",
        "action": "PUSH",
        "reason": "local newer",
        "local": "changed",
        "remote": "same",
    }

    fp = plan_fingerprint(plan)
    # 同样的计划、不同的插入顺序 → 同一个指纹
    reordered = dict(reversed(list(plan.items())))
    assert plan_fingerprint(reordered) == fp
    # SKIP 不参与指纹
    assert plan_fingerprint({"skip.md": plan["skip.md"]}) == plan_fingerprint({})
    # 动作变了 → 指纹变
    changed = dict(plan)
    changed["a.md"] = Classification(Action.CONFLICT)
    assert plan_fingerprint(changed) != fp
    # L / R 参与指纹：内容哈希变了，指纹也得变
    assert plan_fingerprint(plan, {"b.md": "L1"}, {}) != plan_fingerprint(
        plan, {"b.md": "L2"}, {}
    )


def test_side_label_distinguishes_new_from_same():
    assert side_label(False, None, None) == "absent"
    assert side_label(True, "h", None) == "new"  # 从未建立基线
    assert side_label(True, "h", "h") == "same"
    assert side_label(True, "h", "other") == "changed"


# --- 取消 ---


def test_cancel_token_check_is_noop_until_cancelled():
    token = CancelToken()
    token.check()  # 未取消 → 不抛
    assert token.cancelled is False
    token.cancel()
    assert token.cancelled is True
    with pytest.raises(Cancelled):
        token.check()


def test_cancel_token_sleep_is_interruptible():
    token = CancelToken()
    threading.Timer(0.05, token.cancel).start()
    start = time.time()
    with pytest.raises(Cancelled):
        token.sleep(30)
    assert time.time() - start < 5  # 立刻返回，不会睡满 30 秒


def test_cancelled_escapes_execute_plan(tmp_path):
    """取消不能被 execute_plan 的 except Exception 记成「该文件失败」。

    这正是 Cancelled 继承 BaseException 的原因：executor 的兜底只抓 Exception，
    控制流信号必须穿过去，否则取消会被吞掉还留下一条假的失败记录。
    """
    (tmp_path / "a.md").write_text("hello", encoding="utf-8")
    mgr = StateManager(tmp_path)
    mgr.state = {"schema_version": 2, "files": {}, "tombstones": {}}
    mgr.update_file(
        "a.md", {"page_id": "p1", "base_remote_hash": compute_hash("hello")}
    )

    remote = Mock()
    remote.read_page.side_effect = Cancelled()

    with pytest.raises(Cancelled):
        execute_plan({"a.md": Classification(Action.PUSH)}, mgr, remote, tmp_path, {})


def test_remote_waiter_is_cancel_aware():
    token = CancelToken()
    remote = NotionRemote(Mock(), "db", "ds", cancel=token)
    assert remote.limiter.waiter == token.sleep
    # 未传 cancel 时退回 time.sleep
    assert NotionRemote(Mock(), "db", "ds").limiter.waiter is time.sleep


def test_retry_with_backoff_waiter_is_interruptible():
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        retry_with_backoff(lambda: (_ for _ in ()).throw(Exception("429")), waiter=token.sleep)


def test_api_run_cancel_stops_between_files(tmp_path, monkeypatch):
    """取消发生在文件边界：已完成的文件保留，未开始的文件不再执行。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    token = CancelToken()
    seen = []

    plan = {
        "a.md": Classification(Action.PUSH),
        "b.md": Classification(Action.PUSH),
        "c.md": Classification(Action.PUSH),
    }

    def fake_execute(item, *a, **k):
        seen.append(next(iter(item)))
        token.cancel()  # 第一个文件跑完就取消
        return Summary(errors=[])

    from sync_lib import api as api_mod

    _patch_pipeline(monkeypatch, plan)
    monkeypatch.setattr(api_mod, "execute_plan", fake_execute)

    buf = io.StringIO()
    result = api_mod.run(
        "sync", tmp_path, RunOptions(), JsonlReporter(buf, "r-x"), token
    )

    assert result.exit_code == EXIT.CANCELLED
    assert seen == ["a.md"]  # b.md / c.md 没开始
    events = _parse_events(buf.getvalue())
    cancelled = [e for e in events if e["type"] == "run.cancelled"]
    assert cancelled[-1]["data"] == {"done": 1, "remaining": 2}


def test_api_run_expect_plan_mismatch_exits_76(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    mocks, _ = _patch_pipeline(monkeypatch, {"a.md": Classification(Action.PUSH)})

    buf = io.StringIO()
    result = api.run(
        "sync",
        tmp_path,
        RunOptions(expect_plan="deadbeef"),
        JsonlReporter(buf, "r-x"),
        CancelToken(),
    )

    assert result.exit_code == EXIT.PLAN_CHANGED
    mocks["execute_plan"].assert_not_called()
    events = _parse_events(buf.getvalue())
    types = [e["type"] for e in events]
    assert "plan.changed" in types
    assert "file.started" not in types  # 没有进入执行阶段
    # 仍然以 run.finished 收尾：UI 靠它判断进程正常结束（而不是崩了）
    assert types[-1] == "run.finished"
    assert events[-1]["data"]["exit_code"] == EXIT.PLAN_CHANGED


def test_api_run_expect_plan_match_proceeds(tmp_path, monkeypatch):
    """指纹对得上就照常执行。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    plan = {"a.md": Classification(Action.PUSH)}
    mocks, _ = _patch_pipeline(monkeypatch, plan)

    buf = io.StringIO()
    first = api.run(
        "sync",
        tmp_path,
        RunOptions(dry_run=True),
        JsonlReporter(buf, "r-x"),
        CancelToken(),
    )
    assert first.exit_code == EXIT.OK
    fp = [e for e in _parse_events(buf.getvalue()) if e["type"] == "plan"][0]["data"][
        "fingerprint"
    ]

    result = api.run(
        "sync",
        tmp_path,
        RunOptions(expect_plan=fp),
        JsonlReporter(io.StringIO(), "r-y"),
        CancelToken(),
    )

    assert result.exit_code == EXIT.OK
    assert mocks["execute_plan"].called


def test_api_run_only_executes_the_listed_subset(tmp_path, monkeypatch):
    """勾选子集：只执行清单里的路径，没勾的原样留在计划里。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    plan = {
        "a.md": Classification(Action.PUSH),
        "b.md": Classification(Action.PUSH),
        "c.md": Classification(Action.PUSH),
        "d.md": Classification(Action.SKIP),
    }
    mocks, _ = _patch_pipeline(monkeypatch, plan)

    buf = io.StringIO()
    # d.md 也在清单里，但它本来就不执行——不在可执行集合里就自然被忽略
    result = api.run(
        "sync",
        tmp_path,
        RunOptions(only=frozenset({"a.md", "c.md", "d.md"})),
        JsonlReporter(buf, "r-x"),
        CancelToken(),
    )

    assert result.exit_code == EXIT.OK
    assert _executed(mocks) == {"a.md", "c.md"}
    started = [
        e["data"]["path"]
        for e in _parse_events(buf.getvalue())
        if e["type"] == "file.started"
    ]
    assert started == ["a.md", "c.md"]
    assert result.summary["failed"] == 0


def test_api_run_reports_failed_and_needs_attention(tmp_path, monkeypatch):
    """有失败 → 1；计划里有 CONFLICT/BLOCKED → 2；两者都有时 1 优先。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    _patch_pipeline(
        monkeypatch,
        {"a.md": Classification(Action.PUSH)},
        summary=Summary(errors=[{"path": "a.md", "error": "x"}]),
    )
    failed = api.run("sync", tmp_path, RunOptions(), JsonlReporter(io.StringIO(), "r"), CancelToken())
    assert failed.exit_code == EXIT.FAILED

    _patch_pipeline(monkeypatch, {"c.md": Classification(Action.CONFLICT)})
    attention = api.run(
        "sync", tmp_path, RunOptions(), JsonlReporter(io.StringIO(), "r"), CancelToken()
    )
    assert attention.exit_code == EXIT.NEEDS_ATTENTION

    _patch_pipeline(
        monkeypatch,
        {
            "c.md": Classification(Action.CONFLICT),
            "a.md": Classification(Action.PUSH),
        },
        summary=Summary(errors=[{"path": "a.md", "error": "x"}]),
    )
    both = api.run("sync", tmp_path, RunOptions(), JsonlReporter(io.StringIO(), "r"), CancelToken())
    assert both.exit_code == EXIT.FAILED


def test_api_run_emits_per_file_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    _patch_pipeline(monkeypatch, {"a.md": Classification(Action.PUSH)})
    buf = io.StringIO()
    api.run("sync", tmp_path, RunOptions(), JsonlReporter(buf, "r"), CancelToken())

    events = _parse_events(buf.getvalue())
    types = [e["type"] for e in events]
    assert types.index("plan") < types.index("file.started") < types.index("file.finished")
    assert types[-1] == "run.finished"
    started = [e for e in events if e["type"] == "file.started"][0]["data"]
    assert started == {"path": "a.md", "action": "PUSH", "index": 1, "total": 1}
    progress = [e for e in events if e["type"] == "progress"][0]["data"]
    assert progress == {"done": 1, "total": 1}


def test_api_run_skips_lock_for_init(tmp_path, monkeypatch):
    """init 时还没有配置，不能要求先加锁。"""
    from sync_lib import api as api_mod

    def boom(*a, **k):
        raise AssertionError("init 不该去拿锁")

    monkeypatch.setattr(api_mod, "RunLock", boom)
    monkeypatch.setattr(api_mod, "get_token", lambda: (None, "none"))

    buf = io.StringIO()
    result = api_mod.run(
        "init", tmp_path, RunOptions(parent_page_id="p"), JsonlReporter(buf, "r"), CancelToken()
    )

    assert result.exit_code == EXIT.FATAL


def test_api_run_skips_lock_for_pages(tmp_path, monkeypatch):
    """pages 是只读查询，不该拿锁——拿了就会在同步进行中撞上退出码 75。"""
    from sync_lib import api as api_mod

    def boom(*a, **k):
        raise AssertionError("pages 不该去拿锁")

    monkeypatch.setattr(api_mod, "RunLock", boom)
    monkeypatch.setattr(api_mod, "get_token", lambda: (None, "none"))

    buf = io.StringIO()
    result = api_mod.run(
        "pages", tmp_path, RunOptions(), JsonlReporter(buf, "r"), CancelToken()
    )

    assert result.exit_code == EXIT.FATAL


def test_api_run_pages_emits_the_items(tmp_path, monkeypatch):
    from sync_lib import api as api_mod

    monkeypatch.setattr(api_mod, "get_token", lambda: ("tok", "env"))
    monkeypatch.setattr(api_mod, "Client", lambda auth: Mock())
    remote = Mock()
    remote.search_pages.return_value = [{"id": "p1", "title": "工作台", "url": "u"}]
    monkeypatch.setattr(api_mod, "NotionRemote", lambda *a, **k: remote)

    buf = io.StringIO()
    result = api_mod.run(
        "pages", tmp_path, RunOptions(), JsonlReporter(buf, "r"), CancelToken()
    )

    assert result.exit_code == EXIT.OK
    events = _parse_events(buf.getvalue())
    emitted = [e for e in events if e["type"] == "pages"]
    assert emitted[0]["data"]["items"] == [{"id": "p1", "title": "工作台", "url": "u"}]
    assert events[-1]["type"] == "run.finished"
    # 只读命令不进入执行阶段，摘要行不该出现
    assert events[-1]["data"]["executed"] is False


def test_cli_accepts_pages(tmp_path, monkeypatch):
    """argparse 的 choices 里必须有 pages——没有的话界面根本发不出这条命令。"""
    from sync_lib import api as api_mod
    from sync_lib.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_mod, "get_token", lambda: (None, "none"))

    assert main(["pages"]) == EXIT.FATAL


# --- ConsoleReporter：旧 CLI 文本的黄金对照 ---


def test_console_reporter_plan_text_matches_legacy():
    buf = io.StringIO()
    rep = ConsoleReporter(buf)
    rep.emit(
        "plan",
        fingerprint="fp",
        rows=[
            {"path": "a.md", "action": "PUSH", "reason": "local newer"},
            {"path": "b.md", "action": "PULL", "reason": ""},
        ],
        counts={"PUSH": 1, "PULL": 1},
    )
    assert buf.getvalue() == (
        "同步计划：\n"
        "  push           a.md  (local newer)\n"
        "  pull           b.md\n"
    )


def test_console_reporter_empty_plan_text_matches_legacy():
    buf = io.StringIO()
    ConsoleReporter(buf).emit("plan", fingerprint="fp", rows=[], counts={})
    assert buf.getvalue() == "计划为空，无需同步\n"


def test_console_reporter_summary_text_matches_legacy():
    buf = io.StringIO()
    ConsoleReporter(buf).emit(
        "run.finished",
        exit_code=0,
        duration_ms=1,
        executed=True,
        summary={
            "pushed": 1,
            "pulled": 2,
            "created": 3,
            "adopted": 4,
            "archived": 5,
            "deleted": 6,
            "renamed": 7,
            "baselined": 8,
            "conflicts": 0,
            "skipped": 9,
            "failed": 10,
        },
    )
    assert buf.getvalue() == (
        "完成：push=1 pull=2 create=3 adopt=4 archive=5 delete=6 "
        "rename=7 baseline=8 conflict=0 skip=9 errors=10\n"
    )


def test_console_reporter_suppresses_summary_when_not_executed():
    """status / --dry-run / init 不打印摘要行（旧 CLI 在这些分支直接 return）。"""
    buf = io.StringIO()
    ConsoleReporter(buf).emit(
        "run.finished",
        exit_code=0,
        duration_ms=1,
        executed=False,
        summary={"pushed": 0, "failed": 0},
    )
    assert buf.getvalue() == ""


def test_cli_status_text_matches_legacy_shape(tmp_path, monkeypatch, capsys):
    """端到端：status 的文本输出只有计划，没有摘要行。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    _patch_pipeline(monkeypatch, {"a.md": Classification(Action.PUSH)})
    from sync_lib.cli import main

    assert main(["status"]) == EXIT.OK

    out = capsys.readouterr().out
    assert out == "同步计划：\n  push           a.md\n"


def test_cli_sync_text_matches_legacy_shape(tmp_path, monkeypatch, capsys):
    """端到端：sync 的文本输出是计划 + 摘要，顺序与旧 CLI 一致。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    _patch_pipeline(monkeypatch, {"a.md": Classification(Action.PUSH)})
    from sync_lib.cli import main

    assert main(["sync"]) == EXIT.OK

    out = capsys.readouterr().out
    assert out == (
        "同步计划：\n"
        "  push           a.md\n"
        "完成：push=0 pull=0 create=0 adopt=0 archive=0 delete=0 rename=0 "
        "baseline=0 conflict=0 skip=0 errors=0\n"
    )


def test_console_reporter_prints_no_per_file_lines():
    """红线：不传 --json 时不出现逐文件行（旧 CLI 执行期间不说话）。"""
    buf = io.StringIO()
    rep = ConsoleReporter(buf)
    rep.emit("file.started", path="a.md", action="PUSH", index=1, total=1)
    rep.emit("progress", done=1, total=1)
    rep.emit("file.finished", path="a.md", action="PUSH", result="ok")
    assert buf.getvalue() == ""


def test_console_reporter_lock_busy_text_matches_legacy():
    buf = io.StringIO()
    ConsoleReporter(buf).emit("lock.busy", holder={"pid": 1})
    assert buf.getvalue() == "错误：另一个同步进程正在运行\n"


def test_console_reporter_warning_and_log():
    buf = io.StringIO()
    rep = ConsoleReporter(buf)
    rep.emit("warning", code="remote-changes", message="警告：远端有修改，请先 pull 或 sync")
    rep.emit("log", level="info", message="发现 1 个未完成操作，正在恢复…")
    assert buf.getvalue() == (
        "警告：远端有修改，请先 pull 或 sync\n发现 1 个未完成操作，正在恢复…\n"
    )


def test_console_reporter_pages_text():
    """pages 是全新命令，没有旧 CLI 对照，格式自定：一行一个「标题  id」。"""
    buf = io.StringIO()
    ConsoleReporter(buf).emit(
        "pages",
        items=[
            {"id": "p1", "title": "工作台", "url": "u"},
            {"id": "p2", "title": "归档", "url": "u"},
        ],
    )
    assert buf.getvalue() == "工作台  p1\n归档  p2\n"


def test_console_reporter_pages_with_nothing_visible():
    buf = io.StringIO()
    ConsoleReporter(buf).emit("pages", items=[])
    assert "没有可见的页面" in buf.getvalue()


# --- credentials ---


def test_get_token_prefers_env(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "env-token")
    monkeypatch.setattr(credentials.keyring, "get_password", lambda *a: "keyring-token")
    assert credentials.get_token() == ("env-token", "env")


def test_get_token_falls_back_to_keyring(monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.setattr(credentials.keyring, "get_password", lambda *a: "keyring-token")
    assert credentials.get_token() == ("keyring-token", "keyring")


def test_get_token_none_when_unset(monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.setattr(credentials.keyring, "get_password", lambda *a: None)
    assert credentials.get_token() == (None, "none")


def test_save_token_validates_before_storing(monkeypatch):
    """无效 token 不能落盘：验证失败要抛出去，且不调用 set_password。"""
    saved = []
    monkeypatch.setattr(credentials.keyring, "set_password", lambda *a: saved.append(a))

    class _Boom:
        def __init__(self, auth=None):
            raise RuntimeError("401 unauthorized")

    with patch("notion_client.Client", _Boom):
        with pytest.raises(RuntimeError):
            credentials.save_token("bad")
    assert saved == []


def test_save_token_stores_after_validation(monkeypatch):
    saved = []
    monkeypatch.setattr(
        credentials.keyring, "set_password", lambda service, account, token: saved.append((service, account, token))
    )

    class _Ok:
        def __init__(self, auth=None):
            self.users = Mock(me=Mock(return_value={"id": "u1"}))

    with patch("notion_client.Client", _Ok):
        credentials.save_token("good-token")

    assert saved == [(credentials.SERVICE, credentials.ACCOUNT, "good-token")]


def test_delete_token_ignores_missing_entry(monkeypatch):
    def _raise(*a):
        raise PasswordDeleteError("not found")

    monkeypatch.setattr(credentials.keyring, "delete_password", _raise)
    credentials.delete_token()  # 不该抛


# --- executor: resolve（P3，设计 6.5 / 6.6） ---

FORMAT_LF = {
    "encoding": "utf-8",
    "bom": False,
    "eol": "lf",
    "final_newline": False,
}


def _conflict_mgr(tmp_path, path="a.md", **entry):
    """建一个已跟踪、处于冲突态的文件条目。"""
    mgr = StateManager(tmp_path)
    mgr.load()
    base = {
        "page_id": "p1",
        "block_id": "b1",
        "format": dict(FORMAT_LF),
        "status": "conflict",
    }
    base.update(entry)
    mgr.update_file(path, base)
    return mgr


def _write_response(block_id, text):
    """`blocks.update` 的响应体，供 _br_from_update_response 重算 BR。"""
    return {
        "type": "code",
        "id": block_id,
        "code": {"rich_text": [{"plain_text": text}]},
    }


# 形态 a：两端都改了


def test_resolve_keep_local_pushes_and_backs_up_remote(tmp_path):
    (tmp_path / "a.md").write_text("local v2")
    mgr = _conflict_mgr(
        tmp_path,
        base_local_hash=compute_hash("local v1"),
        base_remote_hash=compute_hash("remote v1"),
    )

    remote = Mock()
    remote.read_page.return_value = ("remote v2", "b1", True)
    remote.update_page_content.return_value = _write_response("b1", "local v2")

    result = resolve_conflict(
        "a.md", "local", mgr, remote, tmp_path, True, True, "p1"
    )

    assert result.summary.pushed == 1
    # 本地不动
    assert (tmp_path / "a.md").read_text() == "local v2"
    # 覆盖远端前先备份远端原文（设计 6.5 第 5 条）
    backups = list((tmp_path / ".sync-backup" / "conflicts").glob("a.md.remote-*.md"))
    assert len(backups) == 1
    assert backups[0].read_text() == "remote v2"
    assert result.backup == backups[0].relative_to(tmp_path).as_posix()

    assert remote.update_page_content.call_args[0] == ("p1", "local v2")
    entry = mgr.get_file("a.md")
    assert entry["status"] == "synced"
    assert entry["base_local_hash"] == compute_hash("local v2")
    assert entry["base_remote_hash"] == compute_hash("local v2")


def test_resolve_keep_local_aborts_if_remote_changes_again(tmp_path):
    """重新定基线不是绕过 6.6：从读远端到写远端之间又变了，照样中止。"""
    (tmp_path / "a.md").write_text("local v2")
    mgr = _conflict_mgr(
        tmp_path,
        base_local_hash=compute_hash("local v1"),
        base_remote_hash=compute_hash("remote v1"),
    )

    remote = Mock()
    # 第一次读：解决时备份 + 重定基线用；第二次读：_execute_push 的写前校验
    remote.read_page.side_effect = [
        ("remote v2", "b1", True),
        ("remote v3", "b1", True),
    ]

    with pytest.raises(Exception, match="Remote changed during push"):
        resolve_conflict("a.md", "local", mgr, remote, tmp_path, True, True, "p1")

    remote.update_page_content.assert_not_called()
    assert (tmp_path / "a.md").read_text() == "local v2"
    assert mgr.get_file("a.md")["status"] == "conflict"  # 没解决成功就不放行


def test_resolve_keep_remote_pulls_and_backs_up_local(tmp_path):
    (tmp_path / "a.md").write_text("local v2")
    mgr = _conflict_mgr(
        tmp_path,
        base_local_hash=compute_hash("local v1"),
        base_remote_hash=compute_hash("remote v1"),
    )

    remote = Mock()
    remote.read_page.return_value = ("remote v2", "b2", True)

    result = resolve_conflict(
        "a.md", "remote", mgr, remote, tmp_path, True, True, "p1"
    )

    assert result.summary.pulled == 1
    assert (tmp_path / "a.md").read_text() == "remote v2"
    backups = list((tmp_path / ".sync-backup" / "overwritten").glob("a.md.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "local v2"

    entry = mgr.get_file("a.md")
    assert entry["status"] == "synced"
    assert entry["base_local_hash"] == compute_hash("remote v2")
    assert entry["base_remote_hash"] == compute_hash("remote v2")
    assert entry["block_id"] == "b2"


def test_resolve_keep_merged_pushes_local_like_local(tmp_path):
    (tmp_path / "a.md").write_text("merged result")
    mgr = _conflict_mgr(tmp_path, base_local_hash="x", base_remote_hash="y")

    remote = Mock()
    remote.read_page.return_value = ("remote v2", "b1", True)
    remote.update_page_content.return_value = _write_response("b1", "merged result")

    result = resolve_conflict(
        "a.md", "merged", mgr, remote, tmp_path, True, True, "p1"
    )

    assert result.summary.pushed == 1
    assert remote.update_page_content.call_args[0] == ("p1", "merged result")


# 形态 b：未跟踪，但远端已有同名页且内容不同


def test_resolve_keep_local_on_untracked_conflict_seeds_state(tmp_path):
    """状态里没有这个文件也得能解决：page_id 从远端索引来，补齐 format / block_id。"""
    (tmp_path / "a.md").write_text("local v2")

    mgr = StateManager(tmp_path)
    mgr.load()

    remote = Mock()
    remote.read_page.return_value = ("remote v2", "b9", True)
    remote.update_page_content.return_value = _write_response("b9", "local v2")

    result = resolve_conflict(
        "a.md", "local", mgr, remote, tmp_path, True, True, "p9"
    )

    assert result.summary.pushed == 1
    entry = mgr.get_file("a.md")
    assert entry["page_id"] == "p9"
    assert entry["block_id"] == "b9"
    assert entry["format"]["eol"] == "lf"
    assert entry["status"] == "synced"
    assert entry["base_local_hash"] == compute_hash("local v2")


def test_resolve_keep_remote_on_untracked_conflict(tmp_path):
    (tmp_path / "a.md").write_text("local v2")

    mgr = StateManager(tmp_path)
    mgr.load()

    remote = Mock()
    remote.read_page.return_value = ("remote v2", "b9", True)

    result = resolve_conflict(
        "a.md", "remote", mgr, remote, tmp_path, True, True, "p9"
    )

    assert result.summary.pulled == 1
    assert (tmp_path / "a.md").read_text() == "remote v2"
    # format 没补上 _execute_pull 会 KeyError
    assert mgr.get_file("a.md")["format"]["eol"] == "lf"


# 形态 c：本地删了、远端改了


def test_resolve_keep_local_archives_remote(tmp_path):
    """本地已删 + keep=local = 确认这次删除。"""
    mgr = _conflict_mgr(tmp_path)

    remote = Mock()
    remote.read_page.return_value = ("remote v2", "b1", True)

    result = resolve_conflict(
        "a.md", "local", mgr, remote, tmp_path, False, True, "p1"
    )

    assert result.summary.archived == 1
    remote.archive_page.assert_called_once_with("p1")
    # 归档后状态条目由 tombstone 取代，不能被 status 复活成空壳
    assert "a.md" not in mgr.state["files"]
    assert mgr.state["tombstones"]["a.md"]["page_id"] == "p1"
    # 覆盖远端前仍然先备份远端原文
    backups = list((tmp_path / ".sync-backup" / "conflicts").glob("a.md.remote-*.md"))
    assert backups[0].read_text() == "remote v2"


def test_resolve_keep_remote_recreates_deleted_local_file(tmp_path):
    mgr = _conflict_mgr(tmp_path)

    remote = Mock()
    remote.read_page.return_value = ("remote v2", "b1", True)

    result = resolve_conflict(
        "a.md", "remote", mgr, remote, tmp_path, False, True, "p1"
    )

    assert result.summary.pulled == 1
    assert (tmp_path / "a.md").read_text() == "remote v2"
    assert mgr.get_file("a.md")["status"] == "synced"


def test_resolve_merged_rejected_when_local_deleted(tmp_path):
    mgr = _conflict_mgr(tmp_path)

    with pytest.raises(ValueError, match="没有可合并的本地版本"):
        resolve_conflict("a.md", "merged", mgr, Mock(), tmp_path, False, True, "p1")


# 形态 d：远端删了、本地改了


def test_resolve_keep_local_recreates_remote_page(tmp_path):
    (tmp_path / "a.md").write_text("local v2")
    mgr = _conflict_mgr(tmp_path)

    remote = Mock()
    remote.create_page.return_value = "p-new"
    remote.read_page.return_value = ("local v2", "b-new", True)

    result = resolve_conflict(
        "a.md", "local", mgr, remote, tmp_path, True, False, "p1"
    )

    assert result.summary.created == 1
    entry = mgr.get_file("a.md")
    assert entry["page_id"] == "p-new"
    assert entry["block_id"] == "b-new"
    assert entry["status"] == "synced"
    assert entry["base_local_hash"] == compute_hash("local v2")


def test_resolve_keep_remote_deletes_local_file(tmp_path):
    (tmp_path / "a.md").write_text("local v2")
    mgr = _conflict_mgr(tmp_path)

    result = resolve_conflict(
        "a.md", "remote", mgr, Mock(), tmp_path, True, False, "p1"
    )

    assert result.summary.deleted == 1
    assert not (tmp_path / "a.md").exists()
    # 覆盖本地前先备份（_execute_delete_local 移进 deleted/）
    assert (tmp_path / ".sync-backup" / "deleted" / "a.md").read_text() == "local v2"
    assert "a.md" not in mgr.state["files"]
    assert mgr.state["tombstones"]["a.md"]["page_id"] == "p1"


def test_resolve_merged_rejected_when_remote_deleted(tmp_path):
    (tmp_path / "a.md").write_text("local v2")
    mgr = _conflict_mgr(tmp_path)

    with pytest.raises(ValueError, match="没有可合并的远端版本"):
        resolve_conflict("a.md", "merged", mgr, Mock(), tmp_path, True, False, "p1")


# 入参校验


def test_resolve_rejects_unknown_keep(tmp_path):
    mgr = _conflict_mgr(tmp_path)
    with pytest.raises(ValueError, match="unknown keep"):
        resolve_conflict("a.md", "mine", mgr, Mock(), tmp_path, True, True, "p1")


def test_resolve_rejects_when_neither_side_exists(tmp_path):
    mgr = _conflict_mgr(tmp_path)
    with pytest.raises(ValueError, match="不是冲突"):
        resolve_conflict("a.md", "local", mgr, Mock(), tmp_path, False, False, "p1")


# --- api: resolve 编排 ---


class _EventLog:
    """只记事件不打印，供断言事件序列。"""

    def __init__(self):
        self.events = []

    def emit(self, type, **data):
        self.events.append({"type": type, "data": data})

    def types(self):
        return [e["type"] for e in self.events]

    def first(self, type):
        return next(e["data"] for e in self.events if e["type"] == type)


def _resolve_env(
    tmp_path,
    monkeypatch,
    plan,
    *,
    local=("a.md",),
    state_status="conflict",
    state_page_id="p1",
):
    """搭好 resolve 的编排路径，返回 (mocks, remote)。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    _write_config(tmp_path)

    files = {}
    if state_page_id:
        files["a.md"] = {
            "page_id": state_page_id,
            "block_id": "b1",
            "base_local_hash": "L0",
            "base_remote_hash": "R0",
            "format": dict(FORMAT_LF),
            "status": state_status,
        }
    _write_state(
        tmp_path,
        {
            "schema_version": 2,
            "files": files,
            "tombstones": {},
            "notion": {"database_id": "db", "data_source_id": "ds"},
        },
    )

    mocks, remote = _patch_pipeline(monkeypatch, plan)
    # 本地快照决定 local_exists；_patch_pipeline 默认给空快照
    monkeypatch.setattr(
        api, "scan_local", Mock(return_value=_snapshot({p: "L" for p in local}))
    )
    return mocks, remote


def _run_resolve(monkeypatch, *, return_value=None, side_effect=None, **args):
    """替换 api.resolve_conflict 后跑一次 resolve，返回 (result, mock, log)。"""
    mock = Mock(return_value=return_value, side_effect=side_effect)
    monkeypatch.setattr(api, "resolve_conflict", mock)

    log = _EventLog()
    kwargs = {"path": "a.md", "keep": "local"}
    kwargs.update(args)
    result = _sync(_Args("resolve", **kwargs), reporter=log)
    return result, mock, log


def test_resolve_requires_path(tmp_path, monkeypatch):
    _resolve_env(tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)})
    result, mock, log = _run_resolve(monkeypatch, path=None)

    assert result.exit_code == EXIT.FATAL
    assert log.first("warning")["code"] == "no-path"
    mock.assert_not_called()


def test_resolve_requires_keep(tmp_path, monkeypatch):
    _resolve_env(tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)})
    result, mock, log = _run_resolve(monkeypatch, keep=None)

    assert result.exit_code == EXIT.FATAL
    assert log.first("warning")["code"] == "no-keep"
    mock.assert_not_called()


def test_resolve_rejects_non_conflict_row(tmp_path, monkeypatch):
    """状态是 synced、计划里是 PUSH 的项不该走 resolve。"""
    _resolve_env(
        tmp_path,
        monkeypatch,
        {"a.md": Classification(Action.PUSH)},
        state_status="synced",
    )
    result, mock, log = _run_resolve(monkeypatch)

    assert result.exit_code == EXIT.FATAL
    assert log.first("warning")["code"] == "not-conflict"
    assert "file.started" not in log.types()
    mock.assert_not_called()


def test_resolve_unknown_path(tmp_path, monkeypatch):
    _resolve_env(tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)})
    result, mock, log = _run_resolve(monkeypatch, path="nope.md")

    assert result.exit_code == EXIT.FATAL
    assert log.first("warning")["code"] == "not-in-plan"


def test_resolve_accepts_blocked_row_whose_status_is_conflict(tmp_path, monkeypatch):
    """已标 conflict 的项会再被分类成 BLOCKED，仍然允许解决（幂等重试）。"""
    _resolve_env(
        tmp_path,
        monkeypatch,
        {"a.md": Classification(Action.BLOCKED, "conflict status")},
    )
    outcome = ResolveResult(Summary(errors=[]), None)
    result, mock, log = _run_resolve(monkeypatch, return_value=outcome)

    assert result.exit_code == EXIT.OK
    mock.assert_called_once()


def test_resolve_merged_rejected_when_only_one_side(tmp_path, monkeypatch):
    _resolve_env(
        tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)}, local=()
    )
    result, mock, log = _run_resolve(monkeypatch, keep="merged")

    assert result.exit_code == EXIT.FATAL
    assert log.first("warning")["code"] == "bad-keep"
    mock.assert_not_called()


def test_resolve_emits_full_event_sequence(tmp_path, monkeypatch):
    _resolve_env(tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)})

    outcome = ResolveResult(Summary(errors=[]), "backup/a.md.remote-x.md")
    outcome.summary.pushed = 1

    result, mock, log = _run_resolve(monkeypatch, return_value=outcome)

    assert result.exit_code == EXIT.OK
    assert result.summary["pushed"] == 1

    types = log.types()
    # 界面靠 plan 事件刷新计划表，靠 file.* 定位到行
    assert "plan" in types
    assert types.index("file.started") < types.index("file.finished")
    assert types[-1] == "run.finished"
    assert log.events[-1]["data"]["executed"] is True

    assert log.first("file.started")["action"] == "RESOLVE"
    assert log.first("file.finished")["result"] == "ok"
    assert log.first("progress") == {"done": 1, "total": 1}

    message = log.first("log")["message"]
    assert "已解决" in message
    assert "保留本地" in message
    assert "备份到 backup/a.md.remote-x.md" in message  # 放弃的那侧去了哪儿要说清

    mock.assert_called_once()
    assert mock.call_args[0][1] == "local"


def test_resolve_passes_local_and_remote_existence(tmp_path, monkeypatch):
    """两侧是否存在由编排层判定后传下去，executor 不再自己探。"""
    _resolve_env(
        tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)}, local=()
    )
    outcome = ResolveResult(Summary(errors=[]), None)
    result, mock, log = _run_resolve(monkeypatch, return_value=outcome, keep="remote")

    assert result.exit_code == EXIT.OK
    args = mock.call_args[0]
    assert args[0] == "a.md"
    assert args[5] is False  # local_exists
    assert args[6] is True  # remote_exists（state 里带 page_id，视为 R == BR）
    assert args[7] == "p1"


def test_resolve_merged_message_says_merged(tmp_path, monkeypatch):
    _resolve_env(tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)})
    outcome = ResolveResult(Summary(errors=[]), None)

    result, mock, log = _run_resolve(monkeypatch, return_value=outcome, keep="merged")

    assert result.exit_code == EXIT.OK
    assert "保留合并后的本地" in log.first("log")["message"]


def test_resolve_failure_is_exit_1_with_file_finished(tmp_path, monkeypatch):
    _resolve_env(tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)})

    result, mock, log = _run_resolve(
        monkeypatch, side_effect=Exception("Remote changed during push")
    )

    assert result.exit_code == EXIT.FAILED
    assert result.summary["failed"] == 1
    finished = log.first("file.finished")
    assert finished["result"] == "failed"
    assert "Remote changed" in finished["error"]


def test_cli_parses_resolve_path_and_keep(tmp_path, monkeypatch):
    from sync_lib import cli
    from sync_lib.api import RunResult

    captured = {}

    def _fake_run(command, root, opts, rep, cancel):
        captured["command"] = command
        captured["path"] = opts.path
        captured["keep"] = opts.keep
        return RunResult(EXIT.OK, {})

    monkeypatch.setattr(cli, "run", _fake_run)
    code = cli.main(
        ["resolve", "docs/a.md", "--keep", "remote", "--root", str(tmp_path)]
    )

    assert code == EXIT.OK
    assert captured == {"command": "resolve", "path": "docs/a.md", "keep": "remote"}


def test_cli_resolve_keep_rejects_unknown_value(tmp_path):
    from sync_lib import cli

    with pytest.raises(SystemExit):
        cli.main(["resolve", "a.md", "--keep", "mine", "--root", str(tmp_path)])


# --- backup-only：只落远端副本，供界面做对比（设计 §226） ---


def test_backup_remote_content_writes_copy_and_leaves_state_alone(tmp_path):
    (tmp_path / "a.md").write_text("local v2")
    mgr = _conflict_mgr(tmp_path)
    before = json.loads(json.dumps(mgr.state["files"]))

    remote = Mock()
    remote.read_page.return_value = ("remote v2", "b1", True)

    backup = backup_remote_content("a.md", remote, tmp_path, "p1")

    assert backup.read_text() == "remote v2"
    assert backup.parent == tmp_path / ".sync-backup" / "conflicts"
    # 只看不裁决：状态一个字节都不动
    assert mgr.state["files"] == before
    remote.update_page_content.assert_not_called()
    remote.archive_page.assert_not_called()
    remote.create_page.assert_not_called()


def test_backup_remote_content_rejects_invalid_structure(tmp_path):
    remote = Mock()
    remote.read_page.return_value = ("", "b1", False)

    with pytest.raises(Exception, match="remote invalid-structure"):
        backup_remote_content("a.md", remote, tmp_path, "p1")

    assert not (tmp_path / ".sync-backup").exists()


def test_run_backup_only_writes_copy_without_resolving(tmp_path, monkeypatch):
    _resolve_env(tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)})
    monkeypatch.setattr(api, "resolve_conflict", Mock())
    target = tmp_path / ".sync-backup" / "conflicts" / "a.md.remote-x.md"
    monkeypatch.setattr(api, "backup_remote_content", Mock(return_value=target))

    log = _EventLog()
    result = _sync(_Args("resolve", path="a.md", backup_only=True), reporter=log)

    assert result.exit_code == EXIT.OK
    assert log.first("file.started")["action"] == "BACKUP"
    assert log.first("file.finished")["result"] == "ok"
    assert ".sync-backup/conflicts/a.md.remote-x.md" in log.first("log")["message"]
    api.resolve_conflict.assert_not_called()
    api.backup_remote_content.assert_called_once()


def test_run_backup_only_rejects_keep(tmp_path, monkeypatch):
    _resolve_env(tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)})
    monkeypatch.setattr(api, "backup_remote_content", Mock())

    log = _EventLog()
    result = _sync(
        _Args("resolve", path="a.md", keep="local", backup_only=True), reporter=log
    )

    assert result.exit_code == EXIT.FATAL
    assert log.first("warning")["code"] == "bad-flags"
    api.backup_remote_content.assert_not_called()


def test_run_backup_only_requires_remote_page(tmp_path, monkeypatch):
    """形态 d（远端已删）没有远端副本可拿。"""
    _resolve_env(
        tmp_path,
        monkeypatch,
        {"a.md": Classification(Action.CONFLICT)},
        state_page_id=None,
    )
    monkeypatch.setattr(api, "backup_remote_content", Mock())

    log = _EventLog()
    result = _sync(_Args("resolve", path="a.md", backup_only=True), reporter=log)

    assert result.exit_code == EXIT.FATAL
    assert log.first("warning")["code"] == "no-remote"
    api.backup_remote_content.assert_not_called()


def test_run_backup_only_failure_is_exit_1(tmp_path, monkeypatch):
    _resolve_env(tmp_path, monkeypatch, {"a.md": Classification(Action.CONFLICT)})
    monkeypatch.setattr(
        api,
        "backup_remote_content",
        Mock(side_effect=Exception("remote invalid-structure")),
    )

    log = _EventLog()
    result = _sync(_Args("resolve", path="a.md", backup_only=True), reporter=log)

    assert result.exit_code == EXIT.FAILED
    assert result.summary["failed"] == 1
    assert log.first("file.finished")["result"] == "failed"


def test_cli_parses_backup_only(tmp_path, monkeypatch):
    from sync_lib import cli
    from sync_lib.api import RunResult

    captured = {}

    def _fake_run(command, root, opts, rep, cancel):
        captured["path"] = opts.path
        captured["backup_only"] = opts.backup_only
        captured["keep"] = opts.keep
        return RunResult(EXIT.OK, {})

    monkeypatch.setattr(cli, "run", _fake_run)
    code = cli.main(["resolve", "docs/a.md", "--backup-only", "--root", str(tmp_path)])

    assert code == EXIT.OK
    assert captured == {"path": "docs/a.md", "backup_only": True, "keep": None}
