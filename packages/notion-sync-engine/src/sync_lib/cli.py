"""命令行入口（设计 v2 §7.5）：解析参数、装配 Reporter，业务全在 `api.run`。

向后兼容旧用法：不传 `--root` 取当前目录；不传 `--json` 走 `ConsoleReporter`，
输出与旧 CLI 逐字节一致。
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import uuid
from pathlib import Path

from . import __version__
from .api import RunOptions, run
from .cancel import CancelToken
from .protocol import PROTOCOL_VERSION
from .reporter import ConsoleReporter, JsonlReporter


def _control_loop(cancel: CancelToken) -> None:
    """读 stdin 上的控制命令；父进程消失（EOF）也当作取消。"""
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "cancel":
            cancel.cancel()
    cancel.cancel()  # stdin EOF：父进程已经不在 → 优雅取消


class _EventLogHandler(logging.Handler):
    """把 `logging` 记录转成协议事件，免得日志混进 stdout 破坏 JSONL。"""

    def __init__(self, rep):
        super().__init__()
        self.rep = rep

    def emit(self, record):
        self.rep.emit(
            "log", level=record.levelname.lower(), message=self.format(record)
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="notion-sync", description="Notion 双向同步")
    # 只列引擎真正实现的命令；设计 §7.5 里的 forget/rebuild-state 尚未实现
    p.add_argument(
        "command", choices=["init", "status", "push", "pull", "sync", "resolve", "pages"]
    )
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="resolve 的目标路径（相对同步根目录，POSIX 形式）",
    )
    p.add_argument(
        "--root",
        type=Path,
        default=None,
        help="同步根目录，默认取当前目录（兼容旧用法）",
    )
    p.add_argument("--json", action="store_true", help="stdout 输出 JSON Lines 事件")
    p.add_argument("--control-stdin", action="store_true", help="从 stdin 读取控制命令")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--full", action="store_true")
    p.add_argument("--expect-plan")
    p.add_argument("--parent-page-id")
    p.add_argument(
        "--keep",
        choices=["local", "remote", "merged"],
        help="resolve 的裁决：保留本地 / 保留远端 / 已手动合并到本地",
    )
    p.add_argument(
        "--backup-only",
        action="store_true",
        help="resolve 只把远端内容落盘成对比用的副本，不做裁决",
    )
    a = p.parse_args(argv)

    cancel = CancelToken()

    if a.json:
        proto_out = sys.stdout
        proto_out.reconfigure(encoding="utf-8", newline="\n")
        # 遗留的 print 全部落到 stderr，不污染协议流
        sys.stdout = sys.stderr
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        rep = JsonlReporter(proto_out, run_id=f"r-{uuid.uuid4().hex[:12]}")
        logging.getLogger("sync_lib").addHandler(_EventLogHandler(rep))
        rep.emit(
            "hello",
            engine_version=__version__,
            protocol=PROTOCOL_VERSION,
            pid=os.getpid(),
        )
    else:
        # Windows GBK 控制台无法输出 emoji/中文，重配为 UTF-8
        for _s in (sys.stdout, sys.stderr):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        rep = ConsoleReporter(sys.stdout)
        # 命令行里按 Ctrl+C 同样优雅取消（不抛 KeyboardInterrupt）
        signal.signal(signal.SIGINT, lambda *_: cancel.cancel())

    if a.control_stdin:
        sys.stdin.reconfigure(encoding="utf-8")
        threading.Thread(
            target=_control_loop, args=(cancel,), daemon=True
        ).start()

    opts = RunOptions(
        dry_run=a.dry_run,
        full=a.full,
        expect_plan=a.expect_plan,
        parent_page_id=a.parent_page_id,
        path=a.path,
        keep=a.keep,
        backup_only=a.backup_only,
    )
    return run(a.command, a.root or Path.cwd(), opts, rep, cancel).exit_code


if __name__ == "__main__":
    sys.exit(main())
