"""文件监视与运行调度（设计 2.3；按审查报告 B4/M7 修正）。

- 事件过滤复用 scanner 那套 include/exclude/内置排除（B4 第 2 条）；
- 移动事件两侧都取（B4 第 2 条）；
- 运行结束后按 hash 剔除 PULL 回声（B4 第 3 条）；
- 合并调度：运行中只累积、结束后一次、用户优先、75 退避、定期全量对账（M7）。
"""

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from notion_sync_desktop.services.watcher import (
    FolderWatcher,
    SyncScheduler,
    event_paths,
)
from sync_lib.canonical import compute_hash
from sync_lib.scanner import compile_scan_rules


class _Event:
    """watchdog 事件的替身：只用到这几个字段。"""

    def __init__(self, src, dest=None, is_directory=False):
        self.src_path = str(src)
        self.dest_path = str(dest) if dest else None
        self.is_directory = is_directory


class _FakeEngine(QObject):
    finished = Signal(int, bool)

    def __init__(self):
        super().__init__()
        self.starts: list[tuple] = []
        self.cancelled = 0
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def start(self, command, root, *flags):
        assert not self.running, "scheduler 不该在忙的时候再起一个"
        self.starts.append((command, root, flags))
        self.running = True

    def cancel(self):
        self.cancelled += 1

    def finish(self, code=0):
        self.running = False
        self.finished.emit(code, False)


# ---------------------------------------------------------------- 事件过滤


def test_event_paths_filters_by_scan_rules(tmp_path):
    rules = compile_scan_rules({"include": ["*.md"], "exclude": ["artifacts/"]})

    assert event_paths(_Event(tmp_path / "a.md"), tmp_path, rules) == {"a.md"}
    # include 之外
    assert event_paths(_Event(tmp_path / "notes.txt"), tmp_path, rules) == set()
    # 用户 exclude
    assert event_paths(_Event(tmp_path / "artifacts" / "a.md"), tmp_path, rules) == set()
    # 内置排除：include 配成 ** 时 .sync.lock.json 也不该进来（B4）
    wide = compile_scan_rules({"include": ["**"], "exclude": []})
    assert event_paths(_Event(tmp_path / ".sync.lock.json"), tmp_path, wide) == set()
    assert event_paths(_Event(tmp_path / ".sync-state.json"), tmp_path, wide) == set()
    # 目录事件不关心
    assert event_paths(_Event(tmp_path / "docs", is_directory=True), tmp_path, rules) == set()


def test_event_paths_takes_both_sides_of_move(tmp_path):
    """移动事件同时给出新旧路径，rename 才能被识别（B4 第 2 条）。"""
    rules = compile_scan_rules({"include": ["*.md"], "exclude": []})
    moved = _Event(tmp_path / "old.md", dest=tmp_path / "new.md")

    assert event_paths(moved, tmp_path, rules) == {"old.md", "new.md"}

    # 只有一侧在范围内时，只收那一侧
    outside = _Event(tmp_path / "old.md", dest=tmp_path / "sub" / "new.txt")
    assert event_paths(outside, tmp_path, rules) == {"old.md"}


def test_watcher_debounces_and_merges_bursts(tmp_path, qtbot):
    """连串事件去抖成一次，路径取并集。"""
    watcher = FolderWatcher(debounce_ms=60)
    with qtbot.waitSignal(watcher.changed, timeout=3000) as blocker:
        watcher.note({"a.md"})
        watcher.note({"b.md"})
        watcher.note({"a.md"})

    assert blocker.args[0] == {"a.md", "b.md"}
    assert not watcher._timer.isActive()


def test_watcher_reports_real_file_changes(tmp_path, qtbot):
    """真起 watchdog：写文件 → 收到事件（证明 observer 与 Qt 信号确实接通）。"""
    (tmp_path / ".sync-config.yaml").write_text(
        "include: ['*.md']\nexclude: []\n", encoding="utf-8"
    )
    watcher = FolderWatcher(debounce_ms=50)
    watcher.start(tmp_path, {"include": ["*.md"], "exclude": []})

    try:
        with qtbot.waitSignal(watcher.changed, timeout=8000) as blocker:
            (tmp_path / "hello.md").write_text("hi", encoding="utf-8")
        assert "hello.md" in blocker.args[0]
    finally:
        watcher.stop()


# ---------------------------------------------------------------- 调度


def _write_env(tmp_path, *, include=("*.md",), state=None):
    (tmp_path / ".sync-config.yaml").write_text(
        f"include: {list(include)}\nexclude: []\n", encoding="utf-8"
    )
    (tmp_path / ".sync-state.json").write_text(
        json.dumps(
            state
            or {
                "schema_version": 2,
                "files": {},
                "tombstones": {},
                "notion": {"database_id": "db", "data_source_id": "ds"},
            }
        ),
        encoding="utf-8",
    )


def _make(tmp_path, engine, **kwargs):
    scheduler = SyncScheduler(engine, lambda: str(tmp_path), parent=None, **kwargs)
    scheduler.start()
    return scheduler


def _listing_of(flags) -> set[str]:
    """从启动参数里取增量清单内容。"""
    listing = Path(flags[flags.index("--only-file") + 1])
    return set(listing.read_text(encoding="utf-8").split())


def test_scheduler_starts_incremental_run_with_guards(tmp_path):
    """增量运行：--only-file 列变更路径，且带 --max-destructive 0（B4 第 4 条）。"""
    _write_env(tmp_path)
    engine = _FakeEngine()
    scheduler = _make(tmp_path, engine)

    scheduler.on_paths_changed({"docs/a.md", "b.md"})

    command, root, flags = engine.starts[0]
    assert (command, root) == ("sync", str(tmp_path))
    assert _listing_of(flags) == {"docs/a.md", "b.md"}
    assert flags[flags.index("--max-destructive") + 1] == "0"
    assert "--expect-plan" not in flags
    scheduler.stop()


def test_scheduler_merges_events_arriving_during_a_run(tmp_path):
    """运行中来的事件不改行为，只累积；结束后补一次（M7）。"""
    _write_env(tmp_path)
    engine = _FakeEngine()
    scheduler = _make(tmp_path, engine)

    scheduler.on_paths_changed({"a.md"})
    assert len(engine.starts) == 1

    scheduler.on_paths_changed({"b.md"})  # 运行中的事件：只累积
    assert len(engine.starts) == 1

    engine.finish()
    assert len(engine.starts) == 2
    assert _listing_of(engine.starts[1][2]) == {"b.md"}
    scheduler.stop()


def test_scheduler_drops_pull_echoes_by_hash(tmp_path):
    """运行期间来的事件里，PULL 写盘的回声要剔掉，用户真改的才补跑一轮（B4 第 3 条）。"""
    echo_text = "pulled from remote"
    (tmp_path / "a.md").write_text(echo_text, encoding="utf-8")
    (tmp_path / "b.md").write_text("user edit", encoding="utf-8")
    _write_env(
        tmp_path,
        state={
            "schema_version": 2,
            "files": {
                # a.md 的内容与基线一字不差 → 这是引擎自己写的
                "a.md": {"page_id": "p1", "base_local_hash": compute_hash(echo_text)},
                # b.md 与基线不同 → 用户改的
                "b.md": {"page_id": "p2", "base_local_hash": compute_hash("pushed earlier")},
            },
            "tombstones": {},
            "notion": {"database_id": "db", "data_source_id": "ds"},
        },
    )
    engine = _FakeEngine()
    scheduler = _make(tmp_path, engine)

    scheduler.on_paths_changed({"trigger.md"})
    assert len(engine.starts) == 1

    # 运行期间来的两个事件：a.md 是 PULL 回声，b.md 是用户改动
    scheduler.on_paths_changed({"a.md", "b.md"})
    engine.finish()

    assert len(engine.starts) == 2
    assert _listing_of(engine.starts[1][2]) == {"b.md"}
    scheduler.stop()


def test_scheduler_pause_suppresses_runs_until_resume(tmp_path):
    _write_env(tmp_path)
    engine = _FakeEngine()
    scheduler = _make(tmp_path, engine)

    scheduler.pause()
    scheduler.on_paths_changed({"a.md"})
    assert engine.starts == []

    scheduler.resume()
    assert len(engine.starts) == 1
    assert _listing_of(engine.starts[0][2]) == {"a.md"}
    scheduler.stop()


def test_scheduler_user_takes_priority_over_auto_run(tmp_path):
    """用户操作优先：自动运行正在跑 → 取消它，并丢掉待跑的增量（M7）。"""
    _write_env(tmp_path)
    engine = _FakeEngine()
    scheduler = _make(tmp_path, engine)

    scheduler.on_paths_changed({"a.md"})
    assert engine.cancelled == 0

    assert scheduler.cancel_auto() is True
    assert engine.cancelled == 1

    engine.finish(130)  # 取消后不再自动补一轮
    assert len(engine.starts) == 1

    assert scheduler.cancel_auto() is False  # 没在跑就什么都不做
    scheduler.stop()


def test_scheduler_retries_after_lock_busy(tmp_path, qtbot):
    """外部 CLI 持锁（75）→ 退避后重试同一批路径（M7）。"""
    _write_env(tmp_path)
    engine = _FakeEngine()
    scheduler = _make(tmp_path, engine, backoff_ms=50)

    scheduler.on_paths_changed({"a.md"})
    engine.finish(75)
    assert len(engine.starts) == 1

    qtbot.waitUntil(lambda: len(engine.starts) == 2, timeout=3000)
    assert _listing_of(engine.starts[1][2]) == {"a.md"}
    scheduler.stop()


def test_scheduler_periodic_full_reconcile(tmp_path, qtbot):
    """定期跑一次不带 --only-file 的全量对账，兜住丢失的事件（B4 第 5 条）。"""
    _write_env(tmp_path)
    engine = _FakeEngine()
    scheduler = _make(tmp_path, engine, reconcile_ms=50)

    qtbot.waitUntil(lambda: bool(engine.starts), timeout=3000)
    _command, _root, flags = engine.starts[0]
    assert "--only-file" not in flags
    assert flags[flags.index("--max-destructive") + 1] == "0"
    scheduler.stop()
    engine.finish()
    assert len(engine.starts) == 1  # 停了之后不再有新的对账运行


def test_scheduler_ignores_uninitialized_folder(tmp_path):
    """文件夹还没 init 就不自动跑（否则每动一下就报一次配置错误）。"""
    engine = _FakeEngine()
    scheduler = _make(tmp_path, engine)

    scheduler.on_paths_changed({"a.md"})

    assert engine.starts == []
    scheduler.stop()


@pytest.mark.parametrize("code", [0, 1, 2])
def test_scheduler_does_not_retry_on_normal_exit_codes(tmp_path, code, qtbot):
    _write_env(tmp_path)
    engine = _FakeEngine()
    scheduler = _make(tmp_path, engine, backoff_ms=30)

    scheduler.on_paths_changed({"a.md"})
    scheduler.on_paths_changed({"b.md"})
    engine.finish(code)
    qtbot.wait(120)

    assert len(engine.starts) == 2  # 只有「补一次」，没有额外的退避重试
    assert _listing_of(engine.starts[1][2]) == {"b.md"}
    scheduler.stop()
