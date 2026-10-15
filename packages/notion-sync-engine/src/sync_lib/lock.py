"""运行锁：由操作系统内核持有的文件锁（设计 v2 §4.1 / §7.2）。

进程不论以何种方式结束（正常退出、被 kill、断电），句柄关闭时内核都会
自动释放锁，所以磁盘上残留的 `.sync.lock` 文件没有任何语义。
持有者信息另写 `.sync.lock.json`，只用于诊断显示。
"""

import json
import os
import time
from pathlib import Path

from filelock import FileLock, Timeout


class LockBusy(Exception):
    """另一个同步正在占用同一个根目录。"""

    def __init__(self, holder: dict | None):
        super().__init__("another sync is running")
        self.holder = holder


class RunLock:
    def __init__(self, root: Path, command: str):
        self._lock = FileLock(str(root / ".sync.lock"), timeout=0)
        self._info = root / ".sync.lock.json"
        self._command = command

    def __enter__(self) -> "RunLock":
        try:
            self._lock.acquire()
        except Timeout:
            raise LockBusy(self._read_info()) from None
        self._info.write_text(
            json.dumps(
                {"pid": os.getpid(), "command": self._command, "since": time.time()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return self

    def __exit__(self, *exc) -> None:
        self._info.unlink(missing_ok=True)
        self._lock.release()

    def _read_info(self) -> dict | None:
        try:
            return json.loads(self._info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
