"""运行历史（设计 v2 §5）。

每次运行落一个文件：`%LOCALAPPDATA%\\NotionSync\\runs\\<时间>-<命令>.jsonl`——
内容是那一刻的事件流原样（一行一个协议事件），收尾再补一行 `app.exit` 记真实
退出码。历史是附属品：磁盘写不进去也**绝不**打断同步，最多少一条记录。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from platformdirs import user_data_dir

from .settings import APP_NAME

STAMP_FORMAT = "%Y%m%dT%H%M%S"
# 列表最多读这么多条；文件名以时间戳开头，按名字倒序就是按时间倒序
LIST_LIMIT = 200


def runs_dir() -> Path:
    return Path(user_data_dir(APP_NAME)) / "runs"


def format_stamp(stamp: str) -> str:
    """`20261015T181200` → `2026-10-15 18:12:00`；认不出来就原样返回。"""
    try:
        return datetime.strptime(stamp, STAMP_FORMAT).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return stamp


# ---------------------------------------------------------------- 写


class HistoryWriter:
    """把引擎事件流原样落盘。"""

    def __init__(self, directory: Path | None = None):
        self._dir = directory or runs_dir()
        self._file = None
        self.path: Path | None = None  # 当前这次运行落到了哪个文件

    def write(self, ev: dict) -> None:
        """接在 `EngineClient.event` 上；见到 `run.started` 就开一个新文件。"""
        if ev.get("type") == "run.started":
            # 上一个文件没等到 finish 也得关掉，别把两次运行写进同一个文件
            self._close()
            self._open((ev.get("data") or {}).get("command") or "run")
        self._append(ev)

    def finish(self, exit_code: int, crashed: bool) -> None:
        """接在 `EngineClient.finished` 上：补一行真实退出码再收尾。

        引擎崩溃时协议里的 `run.finished` 可能压根没发出来，而进程退出码
        总是有的——历史页靠这一行才看得出「这次是崩了」。
        """
        self._append(
            {"type": "app.exit", "data": {"exit_code": exit_code, "crashed": crashed}}
        )
        self._close()

    # ---------------------------------------------------------------- 内部

    def _open(self, command: str) -> None:
        path = self._dir / f"{datetime.now().strftime(STAMP_FORMAT)}-{command}.jsonl"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._file = path.open("w", encoding="utf-8")
        except OSError:
            self._file = None
            self.path = None  # 别留着上一次运行的文件名冒充这一次
            return
        self.path = path

    def _append(self, ev: dict) -> None:
        if self._file is None:
            return
        try:
            # 逐行 flush：引擎被强杀时，已经写下的历史仍然读得出来
            self._file.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._file.flush()
        except OSError:
            self._close()

    def _close(self) -> None:
        if self._file is None:
            return
        try:
            self._file.close()
        except OSError:
            # 收尾时刷盘失败没什么可做的了，历史读残缺总比抛进 Qt 事件循环强
            pass
        self._file = None


# ---------------------------------------------------------------- 读


@dataclass
class RunRecord:
    """一次运行的记录；列表和回放共用，读一次就够。"""

    path: Path
    stamp: str
    command: str
    root: str = ""
    exit_code: int | None = None
    crashed: bool = False
    duration_ms: int | None = None
    summary: dict = field(default_factory=dict)
    fingerprint: str = ""
    rows: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)

    @property
    def when(self) -> str:
        return format_stamp(self.stamp)


def load_run(path: Path) -> RunRecord:
    stamp, _, command = path.stem.partition("-")
    record = RunRecord(path=path, stamp=stamp, command=command or path.stem)
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                _apply(record, line)
    except OSError:
        # 文件读不了（被删、被占用）就返回已经读到的部分
        pass
    return record


def list_runs(directory: Path | None = None, limit: int = LIST_LIMIT) -> list[RunRecord]:
    """历次运行，新的在前。目录不存在就是空的。"""
    directory = directory or runs_dir()
    if not directory.is_dir():
        return []
    return [load_run(p) for p in sorted(directory.glob("*.jsonl"), reverse=True)[:limit]]


def _apply(record: RunRecord, line: str) -> None:
    try:
        ev = json.loads(line)
    except ValueError:
        return  # 半行/损坏行跳过，读到的其余部分照样能用
    if not isinstance(ev, dict):
        return
    t, d = ev.get("type"), ev.get("data") or {}
    if t == "run.started":
        record.root = d.get("root", "")
        record.command = d.get("command") or record.command
    elif t == "plan":
        record.fingerprint = d.get("fingerprint", "")
        record.rows = d.get("rows") or []
    elif t == "file.finished":
        record.results.append(d)
    elif t == "run.finished":
        record.exit_code = d.get("exit_code")
        record.duration_ms = d.get("duration_ms")
        record.summary = d.get("summary") or {}
    elif t == "app.exit":
        # 文件里靠后，覆盖 run.finished 的值：进程真实退出码才是准的
        record.exit_code = d.get("exit_code")
        record.crashed = bool(d.get("crashed"))
