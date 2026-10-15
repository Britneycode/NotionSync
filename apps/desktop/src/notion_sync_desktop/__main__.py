"""桌面端入口（设计 v2 §7.10）。"""

import sys

from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, setTheme

from .main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("NotionSync")
    app.setApplicationDisplayName("Notion 同步")
    setTheme(Theme.AUTO)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
