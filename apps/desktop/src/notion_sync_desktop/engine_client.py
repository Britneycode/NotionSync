"""引擎子进程封装（设计 v2 §7.7）。

界面从不 import 引擎的业务代码，只通过子进程 + JSONL 事件流对话：
每次同步起一个进程，stdout 是协议流，stdin 是控制通道（取消），stderr 是诊断。
"""

import json
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from sync_lib.protocol import PROTOCOL_VERSION


def engine_command() -> tuple[str, list[str]]:
    """返回 (可执行文件, 前置参数)。

    打包后同目录下有**两个**引擎：`notion-sync.exe`（控制台，命令行用）和
    `notion-sync-engine.exe`（窗口版）。界面拿后者——界面自己没有控制台，拉起
    控制台程序会被 Windows 新分配一个控制台，每次同步闪一个黑框。见 DECISIONS。
    开发模式用当前解释器跑模块，省得先装一遍命令行入口。
    """
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).with_name("notion-sync-engine.exe")), []
    return sys.executable, ["-m", "sync_lib.cli"]


class EngineClient(QObject):
    """一次运行 = 一个进程；同一时刻只允许一个。"""

    event = Signal(dict)  # 每个协议事件
    diagnostic = Signal(str)  # stderr 的内容与非协议输出
    finished = Signal(int, bool)  # exit_code, crashed
    runningChanged = Signal(bool)

    # 发取消请求后等这么久；超时说明引擎卡在某个请求里，强杀。
    # 强杀在数据上是安全的：写状态是原子的（临时文件 + os.replace），锁由内核释放；
    # 主循环按「每 20 个文件或 2 秒」批量落盘，强杀最多丢这一段的基线，下次运行
    # 按内容重新分类收敛（两侧同改判 BASELINE；CREATE 的 page_id 逐文件落盘，
    # 不会重复建页）。必须大于引擎的 HTTP 超时（api._HTTP_TIMEOUT_MS = 10s），
    # 否则会在请求进行中强杀，白白多丢一批基线。
    CANCEL_GRACE_MS = 15_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._buf = bytearray()
        self._kill_timer = QTimer(self, singleShot=True, interval=self.CANCEL_GRACE_MS)
        self._kill_timer.timeout.connect(self._force_kill)

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def start(self, command: str, root: str, *flags: str) -> None:
        if self.is_running():
            raise RuntimeError("a run is already in progress")
        program, base = engine_command()
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        # 子进程强制 UTF-8，避免 Windows 上按 GBK 解码协议流
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        proc.setProcessEnvironment(env)
        proc.setProgram(program)
        proc.setArguments(
            [*base, command, "--root", root, "--json", "--control-stdin", *flags]
        )
        # 子进程会不会弹控制台窗口：开发模式下它继承启动界面的那个终端，不会弹；
        # 打包后界面拉的是同目录的窗口版引擎（engine_command），也不会弹。
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc, self._buf = proc, bytearray()
        # 先宣告「开始了」再启动：启动失败时 QProcess 会在 proc.start() 里
        # 同步把 errorOccurred 发出来，我们随即发 finished / runningChanged(False)。
        # 要是把这句放在后面，界面收到的顺序就是 False→True，按钮会一直灰着。
        self.runningChanged.emit(True)
        proc.start()

    def cancel(self) -> None:
        """请求协作式取消；引擎不理就等 CANCEL_GRACE_MS 后强杀。"""
        if self.is_running():
            self._proc.write(b'{"type":"cancel"}\n')
            self._kill_timer.start()

    def _force_kill(self) -> None:
        if self.is_running():
            self.diagnostic.emit(
                "取消超时，强制结束引擎（锁由系统释放，未完成的文件下次运行时恢复）\n"
            )
            self._proc.kill()

    def _on_stdout(self) -> None:
        self._buf += bytes(self._proc.readAllStandardOutput())
        # 按行切分，处理半行数据（一次 readyRead 可能只到半个 JSON）
        while (i := self._buf.find(b"\n")) >= 0:
            line = bytes(self._buf[:i]).strip()
            del self._buf[: i + 1]
            self._handle_line(line)

    def _handle_line(self, line: bytes) -> None:
        if not line:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            self.diagnostic.emit(f"[非协议输出] {line.decode('utf-8', 'replace')}\n")
            return
        if ev.get("v") != PROTOCOL_VERSION:
            self.diagnostic.emit(f"协议版本不匹配：{ev.get('v')}\n")
            return
        self.event.emit(ev)

    def _on_stderr(self) -> None:
        text = bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")
        self.diagnostic.emit(text)

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._kill_timer.stop()
        # 进程死了，但管道里可能还有没读走的数据（Qt 不保证 finished 之前已经读完）。
        # 剩下那截没有换行符——引擎写到一半就没了——当作一行处理，
        # 让它以「非协议输出」的形式露在日志里，而不是被悄悄吞掉。
        if self._proc is not None:
            self._on_stdout()
        if self._buf:
            self._handle_line(bytes(self._buf))
            self._buf = bytearray()
        self.finished.emit(code, status == QProcess.CrashExit)
        self._proc.deleteLater()
        self._proc = None
        self.runningChanged.emit(False)

    def _on_error(self, err: QProcess.ProcessError) -> None:
        if err == QProcess.FailedToStart:
            self.diagnostic.emit("引擎启动失败：找不到 notion-sync 程序\n")
            self._kill_timer.stop()
            self.finished.emit(-1, True)
            self._proc = None
            self.runningChanged.emit(False)
