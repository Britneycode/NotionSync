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
    HyperlinkLabel,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SubtitleLabel,
)

from sync_lib.credentials import get_token

from ..engine_client import EngineClient
from ..services.notion_target import CONFIG_NAME, read_target
from ..services.settings import UiSettings
from ..widgets.folder_drop import FolderDropArea
from ..widgets.page_picker import PagePickerDialog

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
    return (Path(folder) / CONFIG_NAME).exists()


class SyncPage(FolderDropArea):
    """拖拽区就是整页；内容放在它的布局里。"""

    # 需要跳到设置页（比如没 token）时发出
    openSettingsRequested = Signal()
    # 「预览计划」不再自己跑命令：切到计划页由它去 `status`（P3）
    previewRequested = Signal()

    def __init__(self, engine: EngineClient, parent=None):
        super().__init__(parent)
        self.setObjectName("syncPage")
        self.engine = engine
        self.settings = UiSettings()
        self._running = False
        # integration 可见的页面（懒加载）：用来把目标 id 换成标题，也给选择器用
        self._pages: list[dict] | None = None
        self._querying = False
        self._pending_picker = False
        self._query: EngineClient | None = None

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
        self.targetCard = self._target_card()
        self.targetCard.setVisible(False)
        root.addWidget(self.targetCard)
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

    def _target_card(self) -> CardWidget:
        """「这个文件夹同步到哪个 Notion 页面」——始终可见，也是换页面的入口。"""
        card = CardWidget(self)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.targetLabel = BodyLabel("", card)
        self.targetLabel.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.openTargetLink = HyperlinkLabel("在 Notion 打开", card)
        self.choosePageButton = PushButton("选择页面…", card)

        layout.addWidget(BodyLabel("同步到", card))
        layout.addWidget(self.targetLabel, 1)
        layout.addWidget(self.openTargetLink)
        layout.addWidget(self.choosePageButton)
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
        self.choosePageButton.clicked.connect(self._choose_page)
        self.syncButton.clicked.connect(lambda: self.run("sync"))
        self.pushButton.clicked.connect(lambda: self.run("push"))
        self.pullButton.clicked.connect(lambda: self.run("pull"))
        self.previewButton.clicked.connect(self.previewRequested.emit)
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
        """按当前文件夹刷新「同步到」那一行，顺带把目标 id 换成标题。"""
        folder = self.folder()
        self.targetCard.setVisible(bool(folder))
        target = read_target(folder) if folder else None
        if target is None:
            self.targetLabel.setText("尚未选择目标页面" if folder else "")
            self.openTargetLink.setVisible(False)
            return
        self.targetLabel.setText(self._page_title(target.parent_page_id) or target.parent_page_id)
        self.openTargetLink.setUrl(target.url)
        self.openTargetLink.setVisible(True)
        # 有目标页面才值得去查标题；还没目标时这次查询留给「选择页面…」自己起
        self._ensure_pages()

    def run(self, command: str, *flags: str) -> None:
        folder = self.folder()
        if not folder:
            self._warn("请先选择文件夹", "拖一个文件夹进来，或点「浏览…」")
            return
        if not Path(folder).is_dir():
            self._warn("文件夹不存在", folder)
            return
        if command != "init" and not is_initialized(folder):
            self._warn("这个文件夹还没初始化", "点「选择页面…」挑一个目标页面")
            self.targetCard.setVisible(True)
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

    # ------------------------------------------------------------ 目标页面

    def _page_title(self, page_id: str) -> str:
        """从缓存的页面列表里找标题；还没查到就是空串。"""
        for page in self._pages or []:
            if page.get("id") == page_id:
                return page.get("title") or ""
        return ""

    def _choose_page(self) -> None:
        """点「选择页面…」：列表没缓存就先查一次，查完自动弹出来。"""
        folder = self.folder()
        if not folder or not Path(folder).is_dir():
            self._warn("请先选择文件夹", "拖一个文件夹进来，或点「浏览…」")
            return
        if self._pages is not None:
            self._open_picker()
            return

        self._pending_picker = True
        self._ensure_pages()
        if self._querying:
            self.choosePageButton.setEnabled(False)
            self.choosePageButton.setText("读取页面…")
            return
        # 查不了（没 token / 引擎起不来）：直接弹空列表，对话框里的提示能说明原因
        self._pending_picker = False
        self._open_picker()

    def _open_picker(self) -> None:
        dialog = PagePickerDialog(self._pages or [], self.window())
        if not dialog.exec():
            return
        page = dialog.selected_page()
        if page is not None:
            self._init_on_page(page["id"])

    def _init_on_page(self, page_id: str) -> None:
        """把目标换成这个页面。已初始化的话先说清楚代价——重新初始化会新建一个库。"""
        if is_initialized(self.folder()) and not self._confirm_reinit():
            return
        self.run("init", "--parent-page-id", page_id)

    def _confirm_reinit(self) -> bool:
        box = MessageBox(
            "更换目标页面",
            "这个文件夹已经初始化过。换页面会在新页面下再建一个「Notion Sync」数据库，"
            "并把所有文件重新推一遍；旧数据库留在原处，不会被删。",
            self.window(),
        )
        box.yesButton.setText("换到新页面")
        box.cancelButton.setText("算了")
        return bool(box.exec())

    # -------------------------------------------------- 页面列表（只读查询）

    def _ensure_pages(self) -> None:
        """懒加载 integration 可见的页面列表；查不到就保持 None，下次再试。"""
        if self._pages is not None or self._querying:
            return
        if get_token()[0] is None:  # 没 token 查不了，也不必打扰用户
            return
        folder = self.folder()
        if not folder or not Path(folder).is_dir():
            return
        self._querying = True
        try:
            self._query_client().start("pages", folder)
        except RuntimeError:
            self._querying = False

    def _query_client(self) -> EngineClient:
        """只读查询走**另一个**引擎进程：不参与「忙」状态，也不进运行历史。

        它跟同步用的那个 `engine` 各持一个 QProcess、互不干扰——引擎侧 `pages`
        不拿运行锁，所以能跟同步并发跑（见 DECISIONS）。
        """
        if self._query is None:
            self._query = EngineClient(self)
            self._query.event.connect(self._on_query_event)
            self._query.finished.connect(self._on_query_finished)
        return self._query

    def _on_query_event(self, ev: dict) -> None:
        if ev.get("type") != "pages":
            return
        self._pages = ev["data"].get("items") or []
        self._querying = False
        self.refresh_folder_state()  # 这次会真的把 id 换成标题
        self._on_query_settled()

    def _on_query_finished(self, exit_code: int, crashed: bool) -> None:
        # 没等到 pages 事件（没 token / 没网 / 引擎挂了）：不缓存，留着下次再试
        self._querying = False
        self._on_query_settled()

    def _on_query_settled(self) -> None:
        self.choosePageButton.setEnabled(not self._running)
        self.choosePageButton.setText("选择页面…")
        if self._pending_picker:
            self._pending_picker = False
            self._open_picker()

    # -------------------------------------------------------- 引擎回调接口

    def set_busy(self, busy: bool) -> None:
        self._running = busy
        for button in (self.syncButton, self.pushButton, self.pullButton, self.previewButton):
            button.setEnabled(not busy)
        self.choosePageButton.setEnabled(not busy and not self._querying)
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
        """进程结束后刷新一次目标显示：init 成功后「同步到」要换成新页面。"""
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
        InfoBar.info(
            title, content, parent=self.window(), position=InfoBarPosition.TOP_RIGHT, duration=4000
        )

    def _warn(self, title: str, content: str) -> None:
        # 挂在顶层窗口上而不是本页：计划页发起的命令也走这里，
        # 提示条跟着页面一起藏起来就白提示了。
        InfoBar.warning(
            title, content, parent=self.window(), position=InfoBarPosition.TOP_RIGHT, duration=6000
        )
