"""拖拽收文件夹（设计 v2 §7.9）。

整页都能接收拖拽；拖进来的必须是本地文件夹才接受——文件、快捷方式、URL 一律忽略。
"""

from pathlib import Path

from PySide6.QtCore import QMimeData, Signal
from PySide6.QtWidgets import QWidget


def first_local_dir(md: QMimeData) -> Path | None:
    """取拖拽内容里的第一个本地文件夹，没有就返回 None。"""
    if not md.hasUrls():
        return None
    for url in md.urls():
        if url.isLocalFile() and (p := Path(url.toLocalFile())).is_dir():
            return p
    return None


class FolderDropArea(QWidget):
    folderDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if first_local_dir(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        # 不实现的话，进入窗口后移动鼠标会收到 ignore，光标不显示「可放置」
        if first_local_dir(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        if p := first_local_dir(e.mimeData()):
            self.folderDropped.emit(str(p))
            e.acceptProposedAction()
