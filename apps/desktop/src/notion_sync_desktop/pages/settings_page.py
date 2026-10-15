"""设置页（设计 v2 §9 P2/P4：token、主题、书签）。

token 存在 Windows 凭据管理器里（`sync_lib.credentials`），存之前会先拿它调一次
Notion 验证——所以保存是网络操作，放线程里做，免得界面卡住。
token 只传给 keyring，不出现在命令行参数里。
"""

from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QThread, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    InfoBar,
    InfoBarPosition,
    ListWidget,
    PasswordLineEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    SwitchButton,
)

import sync_lib
from sync_lib import credentials
from sync_lib.protocol import PROTOCOL_VERSION

from ..services import autostart
from ..services.notion_theme import apply_theme as _apply_notion_theme
from ..services.settings import UiSettings

SOURCE_LABELS = {
    "env": "环境变量 NOTION_TOKEN",
    "keyring": "Windows 凭据管理器",
    "none": "未设置",
}

# 主题下拉的显示名 → 存进 UiSettings 的值
THEME_ITEMS = {"跟随系统": "auto", "浅色": "light", "深色": "dark"}


def apply_theme(name: str) -> None:
    """应用存下来的主题名；认不出来就当「跟随系统」。"""
    _apply_notion_theme(name)


class _TokenSaver(QThread):
    """后台验证并保存 token（`save_token` 会真的请求一次 Notion）。"""

    done = Signal(bool, str)

    def __init__(self, token: str, parent=None):
        super().__init__(parent)
        self._token = token

    def run(self) -> None:
        try:
            credentials.save_token(self._token)
        except Exception as e:  # 网络、401、keyring 不可用都会走到这里
            self.done.emit(False, str(e))
        else:
            self.done.emit(True, "")


class SettingsPage(QWidget):
    # 书签里挑了一个：由主窗口切到「同步」页并把它设成当前文件夹
    openFolderRequested = Signal(str)

    def __init__(self, folder_provider, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._folder_of = folder_provider
        self._saver: _TokenSaver | None = None
        self.settings = UiSettings()

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        root.addWidget(SubtitleLabel("设置", self))
        root.addWidget(self._token_card())
        root.addWidget(self._theme_card())
        root.addWidget(self._startup_card())
        root.addWidget(self._bookmark_card(), 1)
        root.addWidget(self._about_card())

        self.refresh()
        self.refresh_bookmarks()

    # ---------------------------------------------------------------- 界面

    def _token_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.tokenSourceLabel = BodyLabel("", card)
        self.tokenHintLabel = CaptionLabel("", card)
        self.tokenHintLabel.setWordWrap(True)

        self.tokenEdit = PasswordLineEdit(card)
        self.tokenEdit.setPlaceholderText("粘贴 Notion Integration Token（secret_… 或 ntn_…）")
        self.saveButton = PrimaryPushButton("验证并保存", card)
        self.deleteButton = PushButton("删除已保存的 token", card)
        self.saveButton.clicked.connect(self._save_token)
        self.deleteButton.clicked.connect(self._delete_token)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.tokenEdit, 1)
        row.addWidget(self.saveButton)
        row.addWidget(self.deleteButton)

        layout.addWidget(self.tokenSourceLabel)
        layout.addWidget(self.tokenHintLabel)
        layout.addLayout(row)
        return card

    def _theme_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.themeCombo = ComboBox(card)
        self.themeCombo.addItems(list(THEME_ITEMS))
        # 先设好当前值再接信号：否则这一步本身就会当成「用户改了主题」存一次盘
        self.themeCombo.setCurrentIndex(list(THEME_ITEMS.values()).index(self.settings.theme))
        self.themeCombo.currentIndexChanged.connect(self._on_theme_changed)

        layout.addWidget(BodyLabel("主题", card))
        layout.addStretch(1)
        layout.addWidget(self.themeCombo)
        return card

    def _startup_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(BodyLabel("开机自动启动", card))
        text.addWidget(CaptionLabel("登录后待在托盘里，不弹窗", card))

        self.autostartSwitch = SwitchButton(card)
        self.autostartSwitch.setOnText("开")
        self.autostartSwitch.setOffText("关")
        # 先设好当前值再接信号：否则这一步本身就会被当成「用户按了开关」去写注册表
        self.autostartSwitch.setChecked(autostart.is_enabled())
        self.autostartSwitch.checkedChanged.connect(self._on_autostart_toggled)

        layout.addLayout(text)
        layout.addStretch(1)
        layout.addWidget(self.autostartSwitch)
        return card

    def _bookmark_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        self.bookmarkList = ListWidget(card)
        self.bookmarkList.itemDoubleClicked.connect(self._on_bookmark_activated)
        self.bookmarkList.currentRowChanged.connect(self._sync_bookmark_buttons)

        self.addBookmarkButton = PushButton("把当前文件夹加进来", card)
        self.removeBookmarkButton = PushButton("删除选中", card)
        self.addBookmarkButton.clicked.connect(self._add_bookmark)
        self.removeBookmarkButton.clicked.connect(self._remove_bookmark)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.addBookmarkButton)
        row.addWidget(self.removeBookmarkButton)
        row.addStretch(1)

        layout.addWidget(BodyLabel("常用文件夹", card))
        layout.addWidget(CaptionLabel("双击一个就用它同步；列表只显示还在的文件夹", card))
        layout.addWidget(self.bookmarkList, 1)
        layout.addLayout(row)
        return card

    def _about_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)
        layout.addWidget(BodyLabel(f"引擎版本 {sync_lib.__version__}", card))
        layout.addWidget(CaptionLabel(f"事件协议版本 {PROTOCOL_VERSION}", card))
        layout.addWidget(
            CaptionLabel(
                "token 存在 Windows 凭据管理器里；若设置了环境变量 NOTION_TOKEN，"
                "它的优先级更高，界面里保存的值会被忽略。",
                card,
            )
        )
        return card

    # ---------------------------------------------------------------- 状态

    def refresh(self) -> None:
        _token, source = credentials.get_token()
        self.tokenSourceLabel.setText(f"当前 token 来源：{SOURCE_LABELS.get(source, source)}")
        if source == "env":
            self.tokenHintLabel.setText(
                "环境变量优先。要改用界面保存的 token，请先清掉 NOTION_TOKEN 再重启。"
            )
        elif source == "keyring":
            self.tokenHintLabel.setText("已保存在凭据管理器里，同步时自动使用，不用再输入。")
        else:
            self.tokenHintLabel.setText("还没有可用的 token，同步会失败。")
        self.deleteButton.setEnabled(source == "keyring")

    def refresh_bookmarks(self) -> None:
        self.bookmarkList.clear()
        for folder in self.settings.bookmarks:
            self.bookmarkList.addItem(folder)
        self._sync_bookmark_buttons()

    # ------------------------------------------------------------ 用户动作

    def _save_token(self) -> None:
        token = self.tokenEdit.text().strip()
        if not token:
            self._warn("token 不能为空")
            return
        self._set_saving(True)
        self._saver = _TokenSaver(token, self)
        self._saver.done.connect(self._on_saved)
        self._saver.start()

    def _on_saved(self, ok: bool, error: str) -> None:
        self._set_saving(False)
        if ok:
            self.tokenEdit.clear()
            self.refresh()
            self._info("已保存", "token 验证通过，已存入凭据管理器")
        else:
            self._warn(f"保存失败：{error}")

    def _set_saving(self, saving: bool) -> None:
        self.saveButton.setEnabled(not saving)
        self.saveButton.setText("验证中…" if saving else "验证并保存")

    def _delete_token(self) -> None:
        credentials.delete_token()
        self.refresh()
        self._info("已删除", "凭据管理器里的 token 已移除")

    def _on_theme_changed(self, index: int) -> None:
        name = list(THEME_ITEMS.values())[index]
        self.settings.theme = name
        apply_theme(name)

    def _on_autostart_toggled(self, enabled: bool) -> None:
        try:
            if enabled:
                autostart.enable()
            else:
                autostart.disable()
        except OSError as e:  # 组策略之类的会拦住写注册表
            self._warn("改不了开机自启", str(e))
            with QSignalBlocker(self.autostartSwitch):  # 别把「弹回去」再当成一次用户操作
                self.autostartSwitch.setChecked(not enabled)
            return
        self._info("已开启开机自启" if enabled else "已关闭开机自启")

    def _add_bookmark(self) -> None:
        folder = self._folder_of()
        if not folder:
            self._warn("「同步」页还没选文件夹", "先在那里选一个，再回来加书签")
            return
        if not Path(folder).is_dir():
            self._warn("文件夹不存在", folder)
            return
        if self.settings.add_bookmark(folder):
            self.refresh_bookmarks()
            self._info("已加入常用文件夹", folder)
        else:
            self._info("已经在列表里了", folder)

    def _remove_bookmark(self) -> None:
        item = self.bookmarkList.currentItem()
        if item is None:
            return
        self.settings.remove_bookmark(item.text())
        self.refresh_bookmarks()

    def _on_bookmark_activated(self, item) -> None:
        self.openFolderRequested.emit(item.text())

    def _sync_bookmark_buttons(self) -> None:
        self.removeBookmarkButton.setEnabled(self.bookmarkList.currentRow() >= 0)

    # ---------------------------------------------------------------- 杂项

    def _info(self, title: str, content: str = "") -> None:
        InfoBar.success(
            title, content, parent=self, position=InfoBarPosition.TOP_RIGHT, duration=4000
        )

    def _warn(self, title: str, content: str = "") -> None:
        InfoBar.warning(
            title, content, parent=self, position=InfoBarPosition.TOP_RIGHT, duration=6000
        )
