"""主窗口与事件分发（设计 v2 §7.10）。

`_on_event` 是协议事件唯一的落点：一个事件类型对应一个处理。
计划页在 P3 接入；历史页与托盘是 P4。
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
from .models.plan_model import PlanTableModel
from .pages.plan_page import PlanPage
from .pages.settings_page import SettingsPage
from .pages.sync_page import SyncPage


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Notion 同步")
        self.resize(1080, 720)
        self.setMicaEffectEnabled(True)  # Win10 上会自动降级为纯色

        self.engine = EngineClient(self)
        self.plan_model = PlanTableModel(self)
        self.sync_page = SyncPage(self.engine, self)
        # 文件夹只有「同步」页那一份；计划页要用时现取，免得两处各存一份会走岔
        self.plan_page = PlanPage(self.plan_model, self.sync_page.folder, self)
        self.settings_page = SettingsPage(self)
        self.addSubInterface(self.sync_page, FIF.SYNC, "同步")
        self.addSubInterface(self.plan_page, FIF.DOCUMENT, "计划预览")
        self.addSubInterface(
            self.settings_page, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM
        )

        self.engine.event.connect(self._on_event)
        self.engine.diagnostic.connect(self.sync_page.append_diagnostic)
        self.engine.finished.connect(self._on_finished)
        self.engine.runningChanged.connect(self.sync_page.set_busy)
        self.engine.runningChanged.connect(self.plan_page.set_busy)
        self.sync_page.openSettingsRequested.connect(self._show_settings)
        self.sync_page.previewRequested.connect(self._preview_plan)
        # 计划页的命令一律经「同步」页发出，文件夹校验与日志只有一份
        self.plan_page.runRequested.connect(self._run_from_plan)

    # ------------------------------------------------------------ 事件分发

    def _on_event(self, ev: dict) -> None:
        t, d = ev["type"], ev["data"]
        if t == "run.started":
            self.sync_page.append_log("info", f"引擎已启动（{d.get('command')}）")
        elif t == "phase":
            self.sync_page.set_phase(d["name"])
        elif t == "plan":
            self.plan_model.set_plan(d["fingerprint"], d.get("rows") or [])
            self.plan_page.set_counts(d.get("counts") or {})
            self.sync_page.append_log(
                "info", f"计划已更新（{len(d.get('rows') or [])} 项）"
            )
        elif t == "file.started":
            self.sync_page.set_current(d["path"], d["index"], d["total"])
        elif t == "file.finished":
            self.plan_model.update_result(d["path"], d["result"], d.get("error"))
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
            self.sync_page.append_log("warning", "计划已过期，本次未执行；已重新预览")
            InfoBar.warning(
                "计划已过期", "预览之后内容又有变化，已跳过执行并重新预览", parent=self,
                position=InfoBarPosition.TOP_RIGHT, duration=6000,
            )
            self._preview_plan()
        elif t == "run.cancelled":
            self.sync_page.append_log(
                "warning", f"已取消：完成 {d['done']}，剩余 {d['remaining']}"
            )
        elif t == "run.finished":
            self.sync_page.show_summary(d["summary"], d["exit_code"])

    def _on_finished(self, code: int, crashed: bool) -> None:
        self.sync_page.on_run_finished(code)
        # 「打开对比」的 `--backup-only` 跑完了，该拉 diff 了
        self.plan_page.on_run_finished(code)
        if crashed and code != 130:
            InfoBar.error(
                "引擎异常退出", "详细信息见日志", parent=self,
                position=InfoBarPosition.TOP_RIGHT,
            )

    # ---------------------------------------------------------------- 杂项

    def _show_settings(self) -> None:
        self.switchTo(self.settings_page)

    def _preview_plan(self) -> None:
        """「同步」页的「预览计划」：切到计划页并立刻重出一遍。"""
        self.switchTo(self.plan_page)
        self.plan_page.refresh_preview()

    def _run_from_plan(self, command: str, flags: list) -> None:
        """计划页只管表达意图，进程由「同步」页起——它手里有文件夹校验和日志。

        刻意不切页：裁决和执行的结果就显示在计划表格那一行上，
        提示条挂在主窗口上（见 SyncPage 的 `_warn`），在计划页也看得见。
        """
        self.sync_page.run(command, *flags)

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
