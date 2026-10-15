"""主窗口与事件分发（设计 v2 §7.10）。

`_on_event` 是协议事件唯一的落点：一个事件类型对应一个处理。
P2 只挂「同步」和「设置」两页；计划页、历史页、托盘分别是 P3 / P4。
"""

from PySide6.QtWidgets import QApplication
from qfluentwidgets import (
    FluentIcon as FIF,
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
)

from .engine_client import EngineClient
from .pages.settings_page import SettingsPage
from .pages.sync_page import SyncPage


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Notion 同步")
        self.resize(1080, 720)
        self.setMicaEffectEnabled(True)  # Win10 上会自动降级为纯色

        self.engine = EngineClient(self)
        self.sync_page = SyncPage(self.engine, self)
        self.settings_page = SettingsPage(self)
        self.addSubInterface(self.sync_page, FIF.SYNC, "同步")
        self.addSubInterface(
            self.settings_page, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM
        )

        self.engine.event.connect(self._on_event)
        self.engine.diagnostic.connect(self.sync_page.append_diagnostic)
        self.engine.finished.connect(self._on_finished)
        self.engine.runningChanged.connect(self.sync_page.set_busy)
        self.sync_page.openSettingsRequested.connect(self._show_settings)

    # ------------------------------------------------------------ 事件分发

    def _on_event(self, ev: dict) -> None:
        t, d = ev["type"], ev["data"]
        if t == "run.started":
            self.sync_page.append_log("info", f"引擎已启动（{d.get('command')}）")
        elif t == "phase":
            self.sync_page.set_phase(d["name"])
        elif t == "plan":
            self._log_plan(d)
        elif t == "file.started":
            self.sync_page.set_current(d["path"], d["index"], d["total"])
        elif t == "file.finished":
            self.sync_page.append_file_result(d)
        elif t == "progress":
            self.sync_page.set_progress(d["done"], d["total"])
        elif t == "warning":
            self.sync_page.append_log("warning", d["message"])
            InfoBar.warning(
                "警告", d["message"], parent=self, position=InfoBarPosition.TOP_RIGHT, duration=6000
            )
        elif t == "log":
            self.sync_page.append_log(d["level"], d["message"])
        elif t == "lock.busy":
            self.sync_page.append_log("warning", "这个文件夹已有一个同步在运行")
            InfoBar.error(
                "正在同步", "这个文件夹已有一个同步在运行", parent=self,
                position=InfoBarPosition.TOP_RIGHT,
            )
        elif t == "plan.changed":
            self.sync_page.append_log("warning", "计划已变化，本次未执行（P3 会在这里自动重出预览）")
            InfoBar.warning(
                "计划已过期", "预览之后内容又有变化，已跳过执行", parent=self,
                position=InfoBarPosition.TOP_RIGHT, duration=6000,
            )
        elif t == "run.cancelled":
            self.sync_page.append_log(
                "warning", f"已取消：完成 {d['done']}，剩余 {d['remaining']}"
            )
        elif t == "run.finished":
            self.sync_page.show_summary(d["summary"], d["exit_code"])

    def _log_plan(self, d: dict) -> None:
        """P2 没有计划页，把计划按旧界面的做法打进日志。

        P3 接入 PlanPage 后，这里改成只留一行概要。
        """
        rows = d.get("rows") or []
        if not rows:
            self.sync_page.append_log("info", "计划为空，无需同步")
            return
        counts = "，".join(f"{k}={v}" for k, v in sorted(d.get("counts", {}).items()))
        self.sync_page.append_log("info", f"同步计划（{len(rows)} 项）：{counts}")
        for row in rows:
            line = f"  {row['action'].lower():<14} {row['path']}"
            if row.get("reason"):
                line += f"  ({row['reason']})"
            self.sync_page.append_log("info", line)

    def _on_finished(self, code: int, crashed: bool) -> None:
        self.sync_page.on_run_finished(code)
        if crashed and code != 130:
            InfoBar.error(
                "引擎异常退出", "详细信息见日志", parent=self,
                position=InfoBarPosition.TOP_RIGHT,
            )

    # ---------------------------------------------------------------- 杂项

    def _show_settings(self) -> None:
        self.switchTo(self.settings_page)

    def closeEvent(self, e) -> None:
        """同步进行中不让直接关：先确认，再走取消流程，进程退出后才关。"""
        if not self.engine.is_running():
            e.accept()
            return
        e.ignore()
        box = MessageBox(
            "正在同步",
            "关闭窗口会先取消当前同步（当前文件会处理完），确定关闭吗？",
            self,
        )
        if not box.exec():
            return
        self.engine.finished.connect(lambda *_: QApplication.quit())
        self.engine.cancel()
