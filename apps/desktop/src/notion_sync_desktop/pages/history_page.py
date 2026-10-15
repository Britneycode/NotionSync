"""历史页（设计 v2 §5「运行历史」）。

列出历次运行（`run.finished` 的摘要），选中一条就把那次运行的计划表原样摆出来
——包括每一行当时的结果。数据全来自 `services.history` 写的 JSONL，这里只读不写。
"""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QHeaderView, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    PushButton,
    SegmentedWidget,
    SubtitleLabel,
    TableView,
)

from ..models.plan_model import FILTER_GROUPS, ActionFilterProxy, PlanTableModel
from ..services import history
from .sync_page import EXIT_LABELS

COLUMNS = [("when", "时间"), ("command", "命令"), ("root", "文件夹"), ("result", "结果"), ("summary", "摘要")]
LABELS = dict(COLUMNS)

# 摘要列的取值顺序：动作名 → 界面用的短标签（只列非零项，一列塞得下）
SUMMARY_FIELDS = [
    ("pushed", "push"),
    ("pulled", "pull"),
    ("created", "create"),
    ("adopted", "adopt"),
    ("archived", "archive"),
    ("deleted", "delete"),
    ("renamed", "rename"),
    ("baselined", "baseline"),
    ("conflicts", "conflict"),
    ("skipped", "skip"),
    ("failed", "errors"),
]


def summary_text(summary: dict) -> str:
    """只列非零项——历史列表里是一列宽，塞不下 `SyncPage.summary_text` 那份全量。"""
    parts = [f"{label}={summary[key]}" for key, label in SUMMARY_FIELDS if summary.get(key)]
    return "，".join(parts) if parts else "没有动作"


def result_text(record: history.RunRecord) -> str:
    if record.crashed:
        return f"引擎异常退出（{record.exit_code}）"
    if record.exit_code is None:
        return "未完成"  # 进程没走到收尾（被强杀、机器断电）
    return EXIT_LABELS.get(record.exit_code, f"退出码 {record.exit_code}")


class RunListModel(QAbstractTableModel):
    """历次运行；只读。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._runs: list[history.RunRecord] = []

    def set_runs(self, runs: list[history.RunRecord]) -> None:
        self.beginResetModel()
        self._runs = runs
        self.endResetModel()

    def run(self, i: int) -> history.RunRecord:
        return self._runs[i]

    def run_count(self) -> int:
        return len(self._runs)

    # ------------------------------------------------------ QAbstractTableModel

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._runs)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def data(self, idx: QModelIndex, role=Qt.DisplayRole):
        if not idx.isValid() or role != Qt.DisplayRole:
            return None
        record = self._runs[idx.row()]
        key = COLUMNS[idx.column()][0]
        if key == "result":
            return result_text(record)
        if key == "summary":
            return summary_text(record.summary)
        return getattr(record, key, "")

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return LABELS[COLUMNS[section][0]]
        return None


class HistoryPage(QWidget):
    """运行列表 + 计划回放。"""

    def __init__(self, model: RunListModel | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("historyPage")
        self.model = model or RunListModel(self)
        # 回放用自己的一份计划模型：不能跟在跑的预览抢同一张表
        self.plan_model = PlanTableModel(self)
        self._proxy = ActionFilterProxy(self)
        self._proxy.setSourceModel(self.plan_model)

        self._build_ui()
        self._wire()
        self.refresh()

    # ---------------------------------------------------------------- 界面

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        root.addWidget(SubtitleLabel("运行历史", self))
        root.addWidget(
            CaptionLabel("每次同步的事件流都记在本地；点一条看它当时的计划表", self)
        )
        root.addWidget(self._run_card(), 1)
        root.addWidget(self._replay_card(), 2)

    def _run_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        self.table = TableView(card)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        for column in range(len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)  # 摘要吃掉剩余宽度

        row = QHBoxLayout()
        row.setSpacing(8)
        self.folderLabel = CaptionLabel("", card)
        self.folderLabel.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.refreshButton = PushButton("刷新", card)
        row.addWidget(self.folderLabel, 1)
        row.addWidget(self.refreshButton)

        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        return card

    def _replay_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.titleLabel = BodyLabel("还没有选中运行", card)
        self.filter = SegmentedWidget(card)
        for name in FILTER_GROUPS:
            self.filter.addItem(name, name)
        self.filter.setCurrentItem("待处理")
        top.addWidget(self.titleLabel, 1)
        top.addWidget(self.filter)

        self.planTable = TableView(card)
        self.planTable.setModel(self._proxy)
        self.planTable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.planTable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.planTable.setWordWrap(False)
        self.planTable.verticalHeader().hide()
        header = self.planTable.horizontalHeader()
        for column in range(self.plan_model.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)  # 路径列

        layout.addLayout(top)
        layout.addWidget(self.planTable, 1)
        return card

    def _wire(self) -> None:
        self.refreshButton.clicked.connect(self.refresh)
        self.table.clicked.connect(self._on_row_clicked)
        self.filter.currentItemChanged.connect(self._on_filter_changed)
        # 首屏就按「待处理」筛一遍：分段控件是在接线之前设的，信号没人听
        self._on_filter_changed(self.filter.currentRouteKey())

    # ------------------------------------------------------------ 对外接口

    def refresh(self) -> None:
        """重新读一遍历史目录。列表整表替换，回放区跟着清空。"""
        # 保留当前选中的那一条（按文件名认），刷新后还选它
        selected = self._selected_path()
        self.model.set_runs(history.list_runs())
        self.folderLabel.setText(f"记录目录：{history.runs_dir()}")
        self.titleLabel.setText("还没有选中运行")
        self.plan_model.clear()
        if selected is not None:
            self._select_path(selected)

    # ---------------------------------------------------------------- 内部

    def replay(self, record: history.RunRecord) -> None:
        """把一次运行的计划表和当时的结果摆出来。"""
        self.plan_model.set_plan(record.fingerprint, record.rows)
        for d in record.results:
            self.plan_model.update_result(d.get("path", ""), d.get("result", ""), d.get("error"))
        title = f"{record.when}  {record.command}  ——  {result_text(record)}"
        self.titleLabel.setText(title)

    def _selected_path(self):
        index = self.table.currentIndex()
        return None if not index.isValid() else self.model.run(index.row()).path

    def _select_path(self, path) -> None:
        for i in range(self.model.run_count()):
            if self.model.run(i).path == path:
                self.table.selectRow(i)
                self.replay(self.model.run(i))
                return

    def _on_row_clicked(self, index) -> None:
        if index.isValid():
            self.replay(self.model.run(index.row()))

    def _on_filter_changed(self, name: str) -> None:
        self._proxy.set_actions(FILTER_GROUPS.get(name))
