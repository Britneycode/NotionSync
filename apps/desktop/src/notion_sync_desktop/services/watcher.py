"""文件监视与运行调度（设计 2.3；按审查报告 B4/M7 修正）。

两层：

- `FolderWatcher`：watchdog Observer 的 Qt 包装。事件先过 scanner 那套
  include / exclude / 内置排除（B4 第 2 条），去抖后把变更路径集合发出来。
- `SyncScheduler`：把事件合并成「一次运行」（M7）——运行中只累积、结束后补一次；
  运行结束按 hash 剔除 PULL 回声（B4 第 3 条，不用时间戳）；增量运行带
  `--max-destructive 0`，一个破坏性动作都不许做（B4 第 4 条）；
  定时跑一次全量对账，兜住引擎漏掉的事件（B4 第 5 条）。

自动运行永远是 `sync` + `--only-file`：用户没让它做的事（归档、删本地）它不做。
"""

import json
import tempfile
import threading
from pathlib import Path

import yaml
from PySide6.QtCore import QObject, QTimer, Signal
from sync_lib.canonical import compute_hash
from sync_lib.scanner import compile_scan_rules, in_scope
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


def event_paths(event, root, rules) -> set[str]:
    """从 watchdog 事件里取出「在同步范围内」的相对路径。

    移动/重命名事件同时给出新旧路径，两侧都要收——只取一侧的话，本地改名会被
    看成「删了一个、多了一个」（B4 第 2 条）。目录事件不关心。
    """
    if event.is_directory:
        return set()

    paths = set()
    for raw in (event.src_path, getattr(event, "dest_path", None)):
        if not raw:
            continue
        try:
            rel = Path(raw).relative_to(root).as_posix()
        except ValueError:  # 事件落在 root 外（子目录被挂载/软链等）
            continue
        if in_scope(rel, rules):
            paths.add(rel)
    return paths


class _Handler(FileSystemEventHandler):
    """watchdog 回调（跑在 observer 线程里）：过滤后交给 watcher。"""

    def __init__(self, note, root: Path, rules):
        self._note = note
        self._root = root
        self._rules = rules

    def on_any_event(self, event):
        paths = event_paths(event, self._root, self._rules)
        if paths:
            self._note(paths)


class FolderWatcher(QObject):
    """递归监视一个目录，去抖后发出变更路径集合。"""

    changed = Signal(set)
    _ping = Signal()  # 内部：observer 线程 → 主线程（QTimer 只能在主线程动）

    def __init__(self, debounce_ms: int = 1500, parent=None):
        super().__init__(parent)
        self._observer: Observer | None = None
        self._paths: set[str] = set()
        self._lock = threading.Lock()
        self._timer = QTimer(self, singleShot=True, interval=debounce_ms)
        self._timer.timeout.connect(self._flush)
        self._ping.connect(self._timer.start)

    def start(self, root: Path | str, config: dict) -> None:
        self.stop()
        root = Path(root)
        observer = Observer()
        try:
            observer.schedule(
                _Handler(self.note, root, compile_scan_rules(config)), str(root),
                recursive=True,
            )
            observer.start()
        except OSError:
            # 目录不可监视（盘掉线、权限不足）：不自动同步就是了，界面照常用
            observer.stop()
            return
        self._observer = observer

    def stop(self) -> None:
        self._timer.stop()
        with self._lock:
            self._paths.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def note(self, paths: set[str]) -> None:
        """记下一批变更路径。watchdog 线程调用，也会被测试直接调用。"""
        with self._lock:
            self._paths |= set(paths)
        self._ping.emit()

    def _flush(self) -> None:
        with self._lock:
            paths, self._paths = self._paths, set()
        if paths:
            self.changed.emit(paths)


def _local_hash(path: Path) -> str | None:
    """按 scanner 的口径算文件 hash；读不到（刚被删/被占用）返回 None。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return compute_hash(raw.decode("utf-8-sig", errors="ignore"))


class SyncScheduler(QObject):
    """把监听事件合并成一次增量运行（M7）。

    - 运行中只累积路径，结束后补一次；
    - 用户操作优先：`cancel_auto()` 取消正在跑的自动运行并丢掉待跑清单；
    - `pause()` / `resume()`：计划页有待执行预览时暂停，免得指纹失效报 76；
    - 引擎返回 75（外部 CLI 持锁）→ 退避重试；
    - 定期（默认 10 分钟）跑一次全量对账。
    """

    def __init__(
        self,
        engine,
        folder_provider,
        parent=None,
        debounce_ms: int = 1500,
        backoff_ms: int = 5000,
        reconcile_ms: int = 600_000,
    ):
        super().__init__(parent)
        self._engine = engine
        self._folder_provider = folder_provider
        self._pending: set[str] = set()
        self._running_paths: set[str] = set()  # 本轮交给引擎的路径（75 时要重试）
        self._active = False  # 正在跑的是自动运行
        self._paused = False
        self._listing: Path | None = None

        self._watcher = FolderWatcher(debounce_ms, self)
        self._watcher.changed.connect(self.on_paths_changed)

        self._backoff = QTimer(self, singleShot=True, interval=backoff_ms)
        self._backoff.timeout.connect(self._on_backoff)
        self._reconcile = QTimer(self, interval=reconcile_ms)
        self._reconcile.timeout.connect(self.reconcile)
        engine.finished.connect(self._on_finished)

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        folder = self._folder_provider()
        if not folder:
            return
        root = Path(folder)
        config_path = root / ".sync-config.yaml"
        if not config_path.exists():
            return  # 还没 init：不自动跑，否则每动一下就报一次配置错误
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        self._watcher.start(root, config)
        self._reconcile.start()

    def stop(self) -> None:
        self._watcher.stop()
        self._reconcile.stop()
        self._backoff.stop()

    # ------------------------------------------------------------ 调度

    def on_paths_changed(self, paths: set[str]) -> None:
        # 暂停期间照样累积：暂停只是不起运行，恢复后要立刻补上（否则这段改动
        # 得等下一次全量对账才被看见）
        self._pending |= set(paths)
        self._maybe_run()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._maybe_run()

    def is_auto_running(self) -> bool:
        """正在跑的那次是不是自动运行（界面据此区分「谁发出来的计划事件」）。"""
        return self._active

    def cancel_auto(self) -> bool:
        """用户要动手了：取消正在跑的自动运行并丢掉待跑清单，返回是否取消了。"""
        self._pending.clear()
        if self._active and self._engine.is_running():
            self._engine.cancel()
            return True
        return False

    def reconcile(self) -> None:
        """全量对账：不带 --only-file 跑一轮，兜住缓冲区溢出漏掉的事件。"""
        if self._paused or self._engine.is_running():
            return
        self._pending.clear()
        self._start(full=True)

    def _maybe_run(self) -> None:
        if self._paused or not self._pending or self._engine.is_running():
            return  # 忙就等：运行结束后 _on_finished 会再看一眼
        self._start()

    def _start(self, full: bool = False) -> None:
        folder = self._folder_provider()
        if not folder or not (Path(folder) / ".sync-config.yaml").exists():
            return  # 没选文件夹 / 还没 init：不自动跑
        flags: list[str] = []
        self._running_paths = set(self._pending)
        if not full:
            self._listing = self._write_listing(self._running_paths)
            flags += ["--only-file", str(self._listing)]
            self._pending.clear()
        # 自动运行不做任何破坏性动作（B4 第 4 条）
        flags += ["--max-destructive", "0"]
        try:
            self._engine.start("sync", folder, *flags)
        except RuntimeError:
            return  # 刚好被别的东西抢了：等这一轮结束再说
        self._active = True

    def _write_listing(self, paths: set[str]) -> Path:
        """增量清单落到系统临时目录。

        不能写在 root 里：那会触发监视器自己的事件，等于自激（B4）。
        """
        fd, name = tempfile.mkstemp(prefix="notion-sync-watch-", suffix=".txt")
        with open(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(paths)))
        return Path(name)

    def _on_finished(self, code: int, crashed: bool) -> None:
        was_active = self._active
        self._active = False
        self._discard_listing()
        if not was_active:
            return  # 用户自己跑的运行：不掺和

        if code == 75:
            # 锁被外部进程占着，整轮没跑：退避后拿同一批路径再试（M7）
            self._pending |= self._running_paths
            self._backoff.start()
            return

        self._drop_echoes()
        self._maybe_run()

    def _on_backoff(self) -> None:
        if self._pending:
            self._maybe_run()

    def _discard_listing(self) -> None:
        if self._listing is not None:
            self._listing.unlink(missing_ok=True)
            self._listing = None

    def _drop_echoes(self) -> None:
        """剔除 PULL 回声：文件当前内容与 state 里的基线一致 → 那是我们自己写的。

        用 hash 不用时间戳：PULL 写盘、编辑器保存、git checkout 都可能在
        同一秒内发生，时间戳区分不了（B4 第 3 条）。
        """
        if not self._pending:
            return
        folder = self._folder_provider()
        if not folder:
            return
        root = Path(folder)
        try:
            state = json.loads((root / ".sync-state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        files = state.get("files", {})

        self._pending = {
            path
            for path in self._pending
            if not (
                (entry := files.get(path))
                and entry.get("base_local_hash") == _local_hash(root / path)
            )
        }
