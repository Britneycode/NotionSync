"""事件上报（设计 v2 §3.1 / §7.3）。

引擎只认 `Reporter.emit(type, **data)`，不关心事件最终去了哪里：
`JsonlReporter` 写给 UI，`ConsoleReporter` 复刻旧 CLI 的文本输出。
"""

import json
import threading
from datetime import datetime, timezone
from typing import Any, Protocol, TextIO

from .protocol import PROTOCOL_VERSION


class Reporter(Protocol):
    def emit(self, type: str, **data: Any) -> None: ...


class JsonlReporter:
    """每行一个 JSON 事件，写向原始 stdout（设计 §3.2 的事件信封）。"""

    def __init__(self, stream: TextIO, run_id: str):
        self._stream = stream
        self._run_id = run_id
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, type: str, **data: Any) -> None:
        with self._lock:
            self._seq += 1
            event = {
                "v": PROTOCOL_VERSION,
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "run": self._run_id,
                "type": type,
                "data": data,
            }
            self._stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._stream.flush()


class ConsoleReporter:
    """复刻旧 CLI 的文本输出，作为向后兼容的对照基准。

    刻意**不**打印 `file.finished`：旧 CLI 执行期间不输出任何逐文件行，
    加进来会改变既有文本格式（红线「不传 --json 时保持原有文本输出格式不变」）。

    锁被占用的那行沿用旧 CLI 的原文（退出码按设计改成 75，但文本不变）。
    """

    def __init__(self, stream: TextIO):
        self._s = stream

    def emit(self, type: str, **d: Any) -> None:
        if type == "plan":
            self._plan(d)
        elif type == "run.finished":
            # 旧 CLI 在没进入执行阶段时（status / --dry-run / init）不打印摘要
            if d.get("executed"):
                self._summary(d.get("summary") or {})
        elif type == "lock.busy":
            self._s.write("错误：另一个同步进程正在运行\n")
            self._s.flush()
        elif type == "run.cancelled":
            # 旧 CLI 在这里只会抛 KeyboardInterrupt 堆栈，没有既定格式
            self._s.write("已取消\n")
            self._s.flush()
        elif type == "pages":
            # 全新命令，没有旧 CLI 的对照，格式自己定
            self._pages(d)
        elif type in ("warning", "log"):
            self._s.write(f"{d.get('message', '')}\n")
            self._s.flush()

    def _plan(self, d: dict) -> None:
        rows = d.get("rows") or []
        if not rows:
            self._s.write("计划为空，无需同步\n")
        else:
            self._s.write("同步计划：\n")
            for row in rows:
                line = f"  {row['action'].lower():<14} {row['path']}"
                if row.get("reason"):
                    line += f"  ({row['reason']})"
                self._s.write(line + "\n")
        self._s.flush()

    def _pages(self, d: dict) -> None:
        items = d.get("items") or []
        if not items:
            self._s.write(
                "没有可见的页面：先在 Notion 里把目标页面连接给这个 integration\n"
            )
        else:
            for item in items:
                self._s.write(f"{item.get('title', '')}  {item.get('id', '')}\n")
        self._s.flush()

    def _summary(self, s: dict) -> None:
        self._s.write(
            f"完成：push={s.get('pushed', 0)} pull={s.get('pulled', 0)} "
            f"create={s.get('created', 0)} adopt={s.get('adopted', 0)} "
            f"archive={s.get('archived', 0)} delete={s.get('deleted', 0)} "
            f"rename={s.get('renamed', 0)} baseline={s.get('baselined', 0)} "
            f"conflict={s.get('conflicts', 0)} skip={s.get('skipped', 0)} "
            f"errors={s.get('failed', 0)}\n"
        )
        self._s.flush()
