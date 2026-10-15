"""设置页（设计 v2 §9 P2：token 管理）。

token 存在 Windows 凭据管理器里（`sync_lib.credentials`），存之前会先拿它调一次
Notion 验证——所以保存是网络操作，放线程里做，免得界面卡住。
token 只传给 keyring，不出现在命令行参数里。
"""

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    InfoBar,
    InfoBarPosition,
    PasswordLineEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)

import sync_lib
from sync_lib import credentials
from sync_lib.protocol import PROTOCOL_VERSION

SOURCE_LABELS = {
    "env": "环境变量 NOTION_TOKEN",
    "keyring": "Windows 凭据管理器",
    "none": "未设置",
}


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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._saver: _TokenSaver | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        root.addWidget(SubtitleLabel("设置", self))
        root.addWidget(self._token_card())
        root.addWidget(self._about_card())
        root.addStretch(1)

        self.refresh()

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

    # ---------------------------------------------------------------- 杂项

    def _info(self, title: str, content: str = "") -> None:
        InfoBar.success(
            title, content, parent=self, position=InfoBarPosition.TOP_RIGHT, duration=4000
        )

    def _warn(self, title: str) -> None:
        InfoBar.warning(
            title, "", parent=self, position=InfoBarPosition.TOP_RIGHT, duration=6000
        )
