"""计划表格模型（设计 v2 §7.8）。

只做「协议计划行 → 表格单元格」的翻译，不碰引擎、不发命令。
筛选交给 `ActionFilterProxy`（`QSortFilterProxyModel`），页面负责把 view 索引
映射回源行——右键菜单要拿的正是源行。
"""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor

COLUMNS = [
    ("action", "动作"),
    ("path", "路径"),
    ("local", "本地"),
    ("remote", "远端"),
    ("result", "结果"),
]

# 动作 → 颜色（设计 §7.8）
ACTION_COLORS = {
    "PUSH": "#0f6cbd",
    "CREATE_REMOTE": "#0f6cbd",
    "PULL": "#107c10",
    "CONFLICT": "#c42b1c",
    "BLOCKED": "#c42b1c",
    "ARCHIVE_REMOTE": "#8a8886",
    "DELETE_LOCAL": "#8a8886",
    "RENAME": "#8764b8",
}
DEFAULT_ACTION_COLOR = "#605e5c"

# 顶部筛选分段：段名 → 动作集合，None 表示不过滤
FILTER_GROUPS: dict[str, frozenset[str] | None] = {
    "待处理": frozenset(
        {
            "PUSH",
            "PULL",
            "CREATE_REMOTE",
            "RENAME",
            "ADOPT",
            "ARCHIVE_REMOTE",
            "DELETE_LOCAL",
            "BASELINE",
        }
    ),
    "冲突": frozenset({"CONFLICT", "BLOCKED"}),
    "无需处理": frozenset({"SKIP", "UNTRACKED", "UNMANAGED", "DELETE_STATE"}),
    "全部": None,
}

# 引擎给的是短标签（protocol.side_label），表格里换成中文
SIDE_LABELS = {"absent": "无", "new": "新建", "same": "相同", "changed": "已改"}
RESULT_LABELS = {"ok": "成功", "failed": "失败", "skipped": "跳过", "blocked": "阻塞"}
PENDING_LABEL = "待执行"


class PlanTableModel(QAbstractTableModel):
    """计划行；`fingerprint` 是「执行此计划」要带回去的乐观校验依据。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._index: dict[str, int] = {}
        self.fingerprint: str | None = None

    # ------------------------------------------------------------ 数据装载

    def set_plan(self, fingerprint: str, rows: list[dict]) -> None:
        """整表替换。SKIP 行也留着——「无需处理」分段要看得见它们。"""
        self.beginResetModel()
        self.fingerprint = fingerprint
        self._rows = [dict(r, result=PENDING_LABEL) for r in rows]
        self._index = {r["path"]: i for i, r in enumerate(self._rows)}
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self.fingerprint = None
        self._rows = []
        self._index = {}
        self.endResetModel()

    def update_result(self, path: str, result: str, error: str | None = None) -> None:
        """按路径定位到行做局部刷新；不在表里的路径直接忽略。"""
        i = self._index.get(path)
        if i is None:
            return
        self._rows[i]["result"] = RESULT_LABELS.get(result, result)
        self._rows[i]["error"] = error
        self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLUMNS) - 1))

    # ---------------------------------------------------------------- 取用

    def row(self, i: int) -> dict:
        return self._rows[i]

    def row_count(self) -> int:
        return len(self._rows)

    # ------------------------------------------------------ QAbstractTableModel

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def data(self, idx: QModelIndex, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        row = self._rows[idx.row()]
        key = COLUMNS[idx.column()][0]
        if role == Qt.DisplayRole:
            if key in ("local", "remote"):
                return SIDE_LABELS.get(row.get(key, ""), row.get(key, ""))
            return row.get(key, "")
        if role == Qt.ForegroundRole and key == "action":
            return QColor(ACTION_COLORS.get(row["action"], DEFAULT_ACTION_COLOR))
        if role == Qt.TextAlignmentRole and key in ("action", "result"):
            return int(Qt.AlignCenter)
        if role == Qt.ToolTipRole:
            return row.get("error") or row.get("reason") or None
        return None

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section][1]
        return None


class ActionFilterProxy(QSortFilterProxyModel):
    """按动作筛选；`set_actions(None)` 表示放行全部。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._actions: frozenset[str] | None = None

    def set_actions(self, actions: frozenset[str] | None) -> None:
        self._actions = actions
        # Qt 6.10 起 invalidateFilter / invalidateRowsFilter 都标了弃用，改用这对
        self.beginFilterChange()
        self.endFilterChange()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if self._actions is None:
            return True
        return self.sourceModel().row(source_row)["action"] in self._actions
