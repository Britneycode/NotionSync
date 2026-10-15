"""同步页（设计 v2 §7.10）。

整页是拖拽区：拖一个文件夹进来就能同步。页面只负责收集意图并发命令，
真正的活儿全在 `EngineClient` 起的子进程里。
"""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CaptionLabel,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SubtitleLabel,
)

from sync_lib.credentials import get_token

from ..engine_client import EngineClient
from ..services.settings import UiSettings
from ..widgets.folder_drop import FolderDropArea

# 引擎的阶段名 → 界面文案
PHASE_LABELS = {
    "lock": "获取运行锁",
    "recover": "恢复未完成操作",
    "scan": "扫描本地",
    "remote": "读取远端",
    "plan": "生成计划",
    "execute": "执行",
}

# 引擎退出码 → 界面文案（见 sync_lib.protocol.EXIT）
EXIT_LABELS = {
    0: "完成",
    1: "完成，但有文件失败",
    2: "完成，有冲突需要处理",
    3: "失败（配置或 token 有问题）",
    75: "另一个同步正在运行",
    76: "计划已变化，未执行",
    130: "已取消",
    -1: "引擎启动失败",
}


def is_initialized(folder: str | Path) -> bool:
    """这个文件夹是否已经 init 过（引擎会在这里读配置）。"""
    return (Path(folder) / ".sync-config.yaml").exists()


class SyncPage(FolderDropArea):
    """拖拽区就是整页；内容放在它的布局里。"""

    # 需要跳到设置页（比如没 token）时发出
    openSettingsRequested = Signal()

    def __init__(self, engine: EngineClient, parent=None):
        super().__init__(parent)
        self.setObjectName("syncPage")
        self.engine = engine
        self.settings = UiSettings()
        self._running = False

        self._build_ui()
        self._wire()

        last = self.settings.last_folder
        if last:
            self.folderEdit.setText(last)
        self.refresh_folder_state()

    # ---------------------------------------------------------------- 界面

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        root.addWidget(SubtitleLabel("Notion 同步", self))
        hint = CaptionLabel("把文件夹拖到窗口任意位置，或点「浏览…」选择", self)
        root.addWidget(hint)

        root.addWidget(self._folder_card())
        self.initCard = self._init_card()
        self.initCard.setVisible(False)
        root.addWidget(self.initCard)
        root.addWidget(self._action_card())
        root.addWidget(self._progress_card())
        root.addWidget(self._log_card(), 1)

    def _folder_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.folderEdit = LineEdit(card)
        self.folderEdit.setPlaceholderText("同步根目录")
        self.folderEdit.setClearButtonEnabled(True)
        # 让拖到输入框上的文件夹继续冒泡给整页处理，而不是被当成文本拖放
        self.folderEdit.setAcceptDrops(False)

        self.browseButton = PushButton("浏览…", card)
        layout.addWidget(BodyLabel("文件夹", card))
        layout.addWidget(self.folderEdit, 1)
        layout.addWidget(self.browseButton)
        return card

    def _init_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.parentPageEdit = LineEdit(card)
        self.parentPageEdit.setPlaceholderText("Notion 父页面 ID（在页面链接里，32 位十六进制）")
        self.parentPageEdit.setClearButtonEnabled(True)
        self.initButton = PrimaryPushButton("初始化", card)

        layout.addWidget(BodyLabel("尚未初始化", card))
        layout.addWidget(self.parentPageEdit, 1)
        layout.addWidget(self.initButton)
        return card

    def _action_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.syncButton = PrimaryPushButton("双向同步", card)
        self.pushButton = PushButton("推送", card)
        self.pullButton = PushButton("拉取", card)
        self.previewButton = PushButton("预览计划", card)
        self.cancelButton = PushButton("取消", card)
        self.cancelButton.setVisible(False)

        layout.addWidget(self.syncButton)
        layout.addWidget(self.pushButton)
        layout.addWidget(self.pullButton)
        layout.addWidget(self.previewButton)
        layout.addStretch(1)
        layout.addWidget(self.cancelButton)
        return card

    def _progress_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        self.phaseLabel = BodyLabel("就绪", card)
        self.fileLabel = CaptionLabel("", card)
        self.fileLabel.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.progressBar = ProgressBar(card)

        layout.addWidget(self.phaseLabel)
        layout.addWidget(self.progressBar)
        layout.addWidget(self.fileLabel)
        return card

    def _log_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        self.logView = PlainTextEdit(card)
        self.logView.setReadOnly(True)
        self.logView.setMaximumBlockCount(5000)
        self.logView.setPlaceholderText("运行日志")
        self.logView.setAcceptDrops(False)

        layout.addWidget(BodyLabel("日志", card))
        layout.addWidget(self.logView, 1)
        return card

    def _wire(self) -> None:
        self.folderDropped.connect(self.set_folder)
        self.folderEdit.editingFinished.connect(self.refresh_folder_state)
        self.browseButton.clicked.connect(self.pick_folder)
        self.initButton.clicked.connect(self._init_folder)
        self.syncButton.clicked.connect(lambda: self.run("sync"))
        self.pushButton.clicked.connect(lambda: self.run("push"))
        self.pullButton.clicked.connect(lambda: self.run("pull"))
        # 「预览计划」= sync --dry-run：只出计划，不执行
        self.previewButton.clicked.connect(lambda: self.run("sync", "--dry-run"))
        self.cancelButton.clicked.connect(self.engine.cancel)

    # ------------------------------------------------------------ 用户动作

    def set_folder(self, folder: str) -> None:
        self.folderEdit.setText(folder)
        self.refresh_folder_state()

    def pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择同步文件夹", self.folder())
        if folder:
            self.set_folder(folder)

    def refresh_folder_state(self) -> None:
        """按当前文件夹的初始化状态，决定要不要露出「初始化」卡片。"""
        folder = self.folder()
        self.initCard.setVisible(bool(folder) and not is_initialized(folder))

    def run(self, command: str, *flags: str) -> None:
        folder = self.folder()
        if not folder:
            self._warn("请先选择文件夹", "拖一个文件夹进来，或点「浏览…」")
            return
        if not Path(folder).is_dir():
            self._warn("文件夹不存在", folder)
            return
        if command != "init" and not is_initialized(folder):
            self._warn("这个文件夹还没初始化", "填好父页面 ID 后点「初始化」")
            self.initCard.setVisible(True)
            return
        if get_token()[0] is None:
            self._warn("还没有 Notion token", "到「设置」里保存一个，或设置环境变量 NOTION_TOKEN")
            self.openSettingsRequested.emit()
            return

        self.settings.last_folder = folder
        self.logView.clear()
        self.append_log("info", f"开始 {command}：{folder}")
        try:
            self.engine.start(command, folder, *flags)
        except RuntimeError as e:
            self._warn("已有任务在运行", str(e))

    def _init_folder(self) -> None:
        parent = self.parentPageEdit.text().strip()
        if not parent:
            self._warn("请填写父页面 ID", "在 Notion 页面链接末尾，32 位十六进制")
            return
        self.run("init", "--parent-page-id", parent)

    # -------------------------------------------------------- 引擎回调接口

    def set_busy(self, busy: bool) -> None:
        self._running = busy
        for button in (self.syncButton, self.pushButton, self.pullButton, self.previewButton):
            button.setEnabled(not busy)
        self.initButton.setEnabled(not busy)
        self.cancelButton.setVisible(busy)
        if busy:
            # 还不知道总数：先转起来
            self.progressBar.setRange(0, 0)
        else:
            self.progressBar.setRange(0, 1)
            self.progressBar.setValue(1)

    def set_phase(self, name: str) -> None:
        self.phaseLabel.setText(f"阶段：{PHASE_LABELS.get(name, name)}")

    def set_progress(self, done: int, total: int) -> None:
        self.progressBar.setRange(0, max(total, 1))
        self.progressBar.setValue(done)

    def set_current(self, path: str, index: int, total: int) -> None:
        self.fileLabel.setText(f"[{index}/{total}] {path}")

    def append_file_result(self, d: dict) -> None:
        path = d.get("path", "")
        if d.get("result") == "failed":
            self.append_log("error", f"✗ {path} — {d.get('error') or d.get('status') or '失败'}")
        else:
            self.append_log("info", f"✓ {path}")

    def append_log(self, level: str, message: str) -> None:
        prefix = {"error": "[错误] ", "warning": "[警告] "}.get(level, "")
        self.logView.appendPlainText(prefix + message)

    def append_diagnostic(self, text: str) -> None:
        """stderr 与非协议输出：原样进日志，标出来源。"""
        for line in text.splitlines():
            if line.strip():
                self.append_log("warning", f"[引擎] {line}")

    def summary_text(self, summary: dict) -> str:
        return (
            f"push={summary.get('pushed', 0)} pull={summary.get('pulled', 0)} "
            f"create={summary.get('created', 0)} adopt={summary.get('adopted', 0)} "
            f"archive={summary.get('archived', 0)} delete={summary.get('deleted', 0)} "
            f"rename={summary.get('renamed', 0)} baseline={summary.get('baselined', 0)} "
            f"conflict={summary.get('conflicts', 0)} skip={summary.get('skipped', 0)} "
            f"errors={summary.get('failed', 0)}"
        )

    def show_summary(self, summary: dict, exit_code: int) -> None:
        self.phaseLabel.setText(f"阶段：{EXIT_LABELS.get(exit_code, f'退出码 {exit_code}')}")
        self.append_log("info", self.summary_text(summary))

    def on_run_finished(self, exit_code: int) -> None:
        """进程结束后刷新一次初始化状态：init 成功后「初始化」卡片要收起来。"""
        self.refresh_folder_state()
        label = EXIT_LABELS.get(exit_code, f"退出码 {exit_code}")
        if exit_code not in (0,):
            self._warn("同步结束", label)
        else:
            self._info("同步结束", label)

    # ---------------------------------------------------------------- 杂项

    def folder(self) -> str:
        return self.folderEdit.text().strip()

    def _info(self, title: str, content: str) -> None:
        InfoBar.info(title, content, parent=self, position=InfoBarPosition.TOP_RIGHT, duration=4000)

    def _warn(self, title: str, content: str) -> None:
        InfoBar.warning(title, content, parent=self, position=InfoBarPosition.TOP_RIGHT, duration=6000)
