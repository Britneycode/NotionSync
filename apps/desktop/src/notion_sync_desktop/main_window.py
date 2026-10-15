"""主窗口与事件分发（设计 v2 §7.10）。

`_on_event` 是协议事件唯一的落点：一个事件类型对应一个处理。
托盘、运行历史、单实例是 P4。
"""

from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from qfluentwidgets import (
    Action,
    FluentIcon as FIF,
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    SystemTrayMenu,
)

from .engine_client import EngineClient
from .models.plan_model import PlanTableModel
from .pages.history_page import HistoryPage
from .pages.plan_page import PlanPage
from .pages.settings_page import SettingsPage
from .pages.sync_page import EXIT_LABELS, SyncPage
from .services.app_icon import app_icon
from .services.history import HistoryWriter


def tray_available() -> bool:
    """单独抽出来是为了让测试能选「有托盘 / 没托盘」这两条路。"""
    return QSystemTrayIcon.isSystemTrayAvailable()


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Notion 同步")
        self.resize(1080, 720)

        self._quitting = False
        self._tray_hint_shown = False
        self.engine = EngineClient(self)
        self.plan_model = PlanTableModel(self)
        self.history = HistoryWriter()
        self.sync_page = SyncPage(self.engine, self)
        # 文件夹只有「同步」页那一份；要用它的页面现取，免得两处各存一份会走岔
        self.plan_page = PlanPage(self.plan_model, self.sync_page.folder, self)
        self.history_page = HistoryPage(parent=self)
        self.settings_page = SettingsPage(self.sync_page.folder, self)
        self.addSubInterface(self.sync_page, FIF.SYNC, "同步")
        self.addSubInterface(self.plan_page, FIF.DOCUMENT, "计划预览")
        self.addSubInterface(self.history_page, FIF.HISTORY, "历史")
        self.addSubInterface(
            self.settings_page, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM
        )

        # 历史先接：`_on_event` 的 run.started 分支要读 `history.path` 判断这次
        # 有没有落盘，而 Qt 是按连接顺序投递的。
        self.engine.event.connect(self.history.write)
        self.engine.event.connect(self._on_event)
        self.engine.finished.connect(self.history.finish)
        self.engine.finished.connect(self._on_finished)
        self.engine.diagnostic.connect(self.sync_page.append_diagnostic)
        self.engine.runningChanged.connect(self.sync_page.set_busy)
        self.engine.runningChanged.connect(self.plan_page.set_busy)
        self.sync_page.openSettingsRequested.connect(self._show_settings)
        self.sync_page.previewRequested.connect(self._preview_plan)
        # 计划页的命令一律经「同步」页发出，文件夹校验与日志只有一份
        self.plan_page.runRequested.connect(self._run_from_plan)
        self.settings_page.openFolderRequested.connect(self.open_folder)
        self._init_tray()

    # ------------------------------------------------------------ 事件分发

    def _on_event(self, ev: dict) -> None:
        t, d = ev["type"], ev["data"]
        if t == "run.started":
            self.sync_page.append_log("info", f"引擎已启动（{d.get('command')}）")
            if self.history.path is None:
                self.sync_page.append_log("warning", "这次运行没能写进历史（记录目录不可写）")
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
            self._notify(
                EXIT_LABELS.get(d["exit_code"], f"退出码 {d['exit_code']}"),
                self.sync_page.summary_text(d["summary"]),
            )

    def _on_finished(self, code: int, crashed: bool) -> None:
        self.sync_page.on_run_finished(code)
        # 「打开对比」的 `--backup-only` 跑完了，该拉 diff 了
        self.plan_page.on_run_finished(code)
        self.history_page.refresh()
        if crashed and code != 130:
            InfoBar.error(
                "引擎异常退出", "详细信息见日志", parent=self,
                position=InfoBarPosition.TOP_RIGHT,
            )

    # ---------------------------------------------------------------- 托盘

    def _init_tray(self) -> None:
        """托盘是可选能力：拿不到系统托盘（远程会话、精简环境）就退回「关窗即退出」。"""
        if not tray_available():
            self.tray = None
            return
        # 用应用自己的图标：托盘是程序在桌面上的「常驻面孔」，跟窗口图标一致
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("Notion 同步")
        menu = SystemTrayMenu(parent=self)
        menu.addActions(
            [
                Action(FIF.SYNC, "同步当前文件夹", triggered=lambda: self.sync_page.run("sync")),
                Action(FIF.HOME, "打开窗口", triggered=self.show_window),
                Action(FIF.CLOSE, "退出", triggered=self.quit),
            ]
        )
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _notify(self, title: str, content: str) -> None:
        """运行结束的托盘通知；没有托盘就没有通知（界面里照样有摘要）。"""
        if self.tray is not None:
            self.tray.showMessage(title, content, self.tray.icon(), 5000)

    def quit(self) -> None:
        """托盘菜单的「退出」：运行中先确认并取消，等进程退干净再关。"""
        self._quitting = True
        if not self.engine.is_running():
            QApplication.quit()
            return
        box = MessageBox(
            "正在同步", "退出会先取消当前同步（当前文件会处理完），确定退出吗？", self
        )
        if not box.exec():
            self._quitting = False
            return
        self.engine.finished.connect(lambda *_: QApplication.quit())
        self.engine.cancel()

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

    def open_folder(self, folder: str) -> None:
        """换一个同步文件夹（设置页的书签、第二次启动转交过来的参数都走这里）。"""
        self.sync_page.set_folder(folder)
        self.switchTo(self.sync_page)

    def closeEvent(self, e) -> None:
        """有关托盘就把窗口收进托盘（同步继续跑）；没有托盘才真的关。

        真关时若正在同步，先确认再走取消流程，进程退出后才关。
        """
        if self.tray is not None and not self._quitting:
            e.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._notify("还在运行", "窗口已收进托盘，同步继续；要退出请用托盘菜单")
            return
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
