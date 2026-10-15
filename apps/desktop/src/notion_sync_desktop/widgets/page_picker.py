"""页面选择器：从 integration 能访问的页面里挑一个当同步目标。

列表来自引擎的 `pages` 命令（走 Notion 的 search）。search 只返回被显式**连接**给
integration 的页面，所以列表为空不是出错，是还没在 Notion 里连上——提示直接写在对话框里。
"""

from qfluentwidgets import CaptionLabel, ListWidget, MessageBoxBase, SubtitleLabel

PLACEHOLDER_TITLE = "(无标题)"


class PagePickerDialog(MessageBoxBase):
    """选一个页面。用法：`dlg.exec()` 为真后取 `selected_page()`。"""

    def __init__(self, pages: list[dict], parent=None):
        super().__init__(parent)
        self._pages = pages
        self.widget.setMinimumSize(460, 420)
        self.yesButton.setText("使用此页面")
        self.cancelButton.setText("取消")

        self.viewLayout.addWidget(SubtitleLabel("选择目标页面", self))
        caption = CaptionLabel(
            "文件会同步进这个页面下的「Notion Sync」数据库"
            if pages
            else "没有可见的页面：先在 Notion 里把目标页面「连接」给这个 integration",
            self,
        )
        caption.setWordWrap(True)
        self.viewLayout.addWidget(caption)

        self.pageList = ListWidget(self)
        for page in pages:
            self.pageList.addItem(f"{page.get('title') or PLACEHOLDER_TITLE}    {page['id']}")
        self.pageList.itemDoubleClicked.connect(lambda *_: self.accept())
        self.pageList.currentRowChanged.connect(self._sync_yes_button)
        self.viewLayout.addWidget(self.pageList)

        self._sync_yes_button()

    def selected_page(self) -> dict | None:
        row = self.pageList.currentRow()
        return self._pages[row] if 0 <= row < len(self._pages) else None

    def validate(self) -> bool:
        return self.selected_page() is not None

    def _sync_yes_button(self, *_args) -> None:
        # 没选中就没得确认；列表为空时连点都点不动，只能取消
        self.yesButton.setEnabled(self.selected_page() is not None)
