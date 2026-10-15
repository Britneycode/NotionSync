"""桌面端入口（设计 v2 §7.10）。

启动顺序有讲究：先认领单实例，确认自己不是「第二个」再建窗口——
否则第二个实例会白建一个窗口又被关掉。
"""

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow
from .pages.settings_page import apply_theme
from .services.settings import UiSettings
from .services.single_instance import SingleInstance, server_name


def folder_argument(argv: list[str]) -> str:
    """命令行里第一个存在的目录参数。

    `notion-sync-desktop D:\\Demo\\003` 这种用法：第一次启动就打开它，
    后来者则把这一个参数转交给已经开着的窗口。
    """
    for arg in argv[1:]:
        path = Path(arg)
        if path.is_dir():
            return str(path.resolve())
    return ""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("NotionSync")
    app.setApplicationDisplayName("Notion 同步")
    apply_theme(UiSettings().theme)

    folder = folder_argument(argv)
    instance = SingleInstance(server_name(), parent=app)
    if instance.forward(folder):
        return 0  # 已经有一个窗口在跑，参数交给它了，这边直接退

    win = MainWindow()

    def on_message(message: dict) -> None:
        if message.get("folder"):
            win.open_folder(message["folder"])
        win.show_window()

    instance.messageReceived.connect(on_message)
    instance.listen()  # 抢不到监听口也不影响用，只是没有单实例
    win.show()
    if folder:
        win.sync_page.set_folder(folder)
    # 有托盘时关掉窗口不该结束进程（同步还在托盘后面跑）
    app.setQuitOnLastWindowClosed(win.tray is None)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
