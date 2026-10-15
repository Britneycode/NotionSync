"""应用图标：窗口 / 任务栏与系统托盘共用同一份。

exe 里嵌的那份（spec 的 `icon=`）只给资源管理器与快捷方式看；窗口和任务栏走的是
`WM_SETICON`，得 `QApplication.setWindowIcon()` 显式设，Qt 不会去读 exe 里的资源。
图标文件本身还要作为数据文件打进包里（spec 的 `datas`），否则打包后这里指到空路径，
`QIcon` 只会静默给一个空图标，界面就退回 Windows 的通用图标（见 RISKS）。
"""

import sys
from pathlib import Path

from PySide6.QtGui import QIcon


def icon_path() -> Path:
    """图标文件的落点：打包后在 `_internal/notion_sync_desktop/resources/`，源码里就在包目录旁边。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "notion_sync_desktop" / "resources" / "icon.ico"
    return Path(__file__).resolve().parent.parent / "resources" / "icon.ico"


def app_icon() -> QIcon:
    return QIcon(str(icon_path()))
