"""计划预览页（设计 v2 §7.10 / §226）。

预览走 `status --json`：引擎照常分类、发 `plan` 事件，表格由 `PlanTableModel` 承载。
「执行此计划」带着当前指纹跑 `sync --expect-plan <指纹>`；引擎重新分类后若发现不一致，
发 `plan.changed` 并以 76 结束且**不写任何东西**，这里收到后自动重出预览。

冲突行的右键菜单给出三种裁决（`resolve <path> --keep …`）和「打开对比」。
页面本身不起进程——所有命令都通过 `runRequested` 交给「同步」页统一发出，
那里的文件夹校验、token 提示和日志就都只有一份。
"""

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QHeaderView, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    CaptionLabel,
    CardWidget,
    CheckBox,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    SegmentedWidget,
    SubtitleLabel,
    TableView,
)

from ..models.plan_model import FILTER_GROUPS, ActionFilterProxy, PlanTableModel
from ..services.vscode import CodeNotFound, open_diff

# 能右键裁决的动作：CONFLICT 是常态，BLOCKED 里也可能躺着一条
# 「已标 conflict」的行（引擎两种都收，真不是冲突它会给出说明）。
CONFLICT_ACTIONS = frozenset({"CONFLICT", "BLOCKED"})


def newest_remote_backup(root: Path, path: str) -> Path | None:
    """取这个文件最新的一份远端副本。

    落点与命名由引擎的 `executor._write_remote_backup` 决定：
    `.sync-backup/conflicts/<path>.remote-<时间戳><扩展名>`。时间戳是
    `%Y%m%dT%H%M%S`，按名字排序就是按时间排序。这是界面唯一依赖那个约定的地方，
    「打开对比」刚刚跑完 `--backup-only`，所以最新的那份就是它写的。
    """
    directory = root / ".sync-backup" / "conflicts" / Path(path).parent
    if not directory.is_dir():
        return None
    name, suffix = Path(path).name, Path(path).suffix
    prefix = f"{name}.remote-"
    matches = [
        p
        for p in directory.iterdir()
        if p.is_file() and p.name.startswith(prefix) and p.name.endswith(suffix)
    ]
    return max(matches, key=lambda p: p.name) if matches else None


class PlanPage(QWidget):
    """计划表格 + 筛选 + 执行/裁决入口。"""

    # (命令, 附加参数)；由主窗口转给「同步」页去起进程
    runRequested = Signal(str, list)

    def __init__(self, model: PlanTableModel, folder_provider, parent=None):
        super().__init__(parent)
        self.setObjectName("planPage")
        self._model = model
        self._folder_of = folder_provider
        self._busy = False
        self._pending_diff: str | None = None
        # 勾选清单的临时文件，跑完就删（见 _execute_plan / on_run_finished）
        self._only_file: Path | None = None

        self._proxy = ActionFilterProxy(self)
        self._proxy.setSourceModel(model)

        self._build_ui()
        self._wire()
        # 首屏就按「待处理」筛一遍：分段控件是在接线之前设的，信号没人听
        self._on_filter_changed(self.filter.currentRouteKey())
        self._sync_buttons()

    # ---------------------------------------------------------------- 界面

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        root.addWidget(SubtitleLabel("计划预览", self))
        root.addWidget(
            CaptionLabel(
                "先「重新预览」看清这一轮要做什么，再点「执行此计划」——只执行勾上的行，"
                "不想动的取消勾选即可；预览之后内容又变了会被拒绝执行，并自动重出预览",
                self,
            )
        )
        root.addWidget(self._toolbar_card())
        root.addWidget(self._table_card(), 1)

    def _toolbar_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.filter = SegmentedWidget(card)
        for name in FILTER_GROUPS:
            self.filter.addItem(name, name)
        self.filter.setCurrentItem("待处理")

        self.countsLabel = CaptionLabel("还没有计划", card)
        self.countsLabel.setTextInteractionFlags(Qt.TextSelectableByMouse)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.previewButton = PushButton("重新预览", card)
        self.selectAll = CheckBox("全选", card)
        self.selectAll.setTristate(True)
        self.executeButton = PrimaryPushButton("执行此计划", card)
        self.executeButton.setToolTip("只执行勾上的行；全部勾上就是整份计划")
        buttons.addWidget(self.previewButton)
        buttons.addWidget(self.selectAll)
        buttons.addStretch(1)
        buttons.addWidget(self.executeButton)

        layout.addWidget(self.filter)
        layout.addWidget(self.countsLabel)
        layout.addLayout(buttons)
        return card

    def _table_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        self.table = TableView(card)
        self.table.setModel(self._proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        for column in range(self._model.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)  # 路径列吃掉剩余宽度
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)

        layout.addWidget(self.table, 1)
        layout.addWidget(
            CaptionLabel("右键冲突行可以裁决：保留本地 / 保留远端 / 已手动合并 / 打开对比", card)
        )
        return card

    def _wire(self) -> None:
        self.filter.currentItemChanged.connect(self._on_filter_changed)
        self.previewButton.clicked.connect(self.refresh_preview)
        self.executeButton.clicked.connect(self._execute_plan)
        self.selectAll.clicked.connect(self._toggle_all)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        # 计划一换，指纹就有了，「执行此计划」和「全选」该跟着走
        self._model.modelReset.connect(self._sync_buttons)
        self._model.checkedChanged.connect(self._sync_buttons)

    # ------------------------------------------------------------ 对外接口

    def refresh_preview(self) -> None:
        """重出一遍计划。真正的文件夹/token 校验在「同步」页那一份里。"""
        self.runRequested.emit("status", [])

    def set_counts(self, counts: dict) -> None:
        """`plan` 事件附带的动作计数，直接显示，免得再扫一遍表格。"""
        if not counts:
            self.countsLabel.setText("计划为空，没有需要同步的内容")
            return
        detail = "，".join(f"{k.lower()}={v}" for k, v in sorted(counts.items()))
        total = sum(counts.values())
        self.countsLabel.setText(f"共 {total} 项：{detail}")

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync_buttons()

    def on_run_finished(self, exit_code: int) -> None:
        """`--backup-only` 跑完就把对比窗口拉起来；顺便清掉勾选清单。"""
        if self._only_file is not None:
            self._only_file.unlink(missing_ok=True)
            self._only_file = None
        path, self._pending_diff = self._pending_diff, None
        if path is None or exit_code != 0:
            return  # 失败时引擎已经报过错了，别再弹一次
        self._show_diff(path)

    # ------------------------------------------------------------ 用户动作

    def _execute_plan(self) -> None:
        fingerprint = self._model.fingerprint
        if not fingerprint:
            self._warn("还没有计划", "先点「重新预览」生成一份")
            return
        flags = ["--expect-plan", fingerprint]
        picked = self._model.checked_paths()
        if picked is not None:  # 全勾时返回 None：不带清单，走原来的全量
            self._only_file = self._write_only_file(picked)
            flags += ["--only-file", str(self._only_file)]
        self.runRequested.emit("sync", flags)

    def _toggle_all(self) -> None:
        """「全选」被点：还没全勾就全勾上，已经全勾就全取消。"""
        self._model.set_all_checked(self._model.checked_paths() is not None)

    def _write_only_file(self, paths: frozenset[str]) -> Path:
        """勾选清单落成临时文件——路径多了塞不进命令行，交给引擎自己读。

        放系统临时目录，不落同步根目录（那儿放什么都会被扫描当成文件）。
        """
        fd, name = tempfile.mkstemp(prefix="notion-sync-only-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(sorted(paths)))
        return Path(name)

    def _resolve(self, path: str, keep: str) -> None:
        self.runRequested.emit("resolve", [path, "--keep", keep])

    def _open_diff(self, path: str) -> None:
        """先让引擎把远端内容落盘成副本，跑完再拉 diff（见 on_run_finished）。"""
        if self._pending_diff is not None:
            self._warn("正在准备对比", "上一次「打开对比」还没跑完")
            return
        self._pending_diff = path
        self.runRequested.emit("resolve", [path, "--backup-only"])

    def _show_diff(self, path: str) -> None:
        root = Path(self._folder_of())
        backup = newest_remote_backup(root, path)
        if backup is None:
            self._warn("没有找到远端副本", "引擎说写好了，但 .sync-backup/conflicts 下没有它")
            return
        try:
            open_diff(root / path, backup)
        except CodeNotFound:
            self._warn("没找到 VS Code", "装一个 VS Code，或把 code 命令加进 PATH")

    # ---------------------------------------------------------------- 内部

    def _on_filter_changed(self, name: str) -> None:
        self._proxy.set_actions(FILTER_GROUPS.get(name))

    def _on_context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        menu = self.menu_for_row(self._model.row(self._proxy.mapToSource(index).row()))
        if menu is not None:
            menu.exec(self.table.viewport().mapToGlobal(pos))

    def menu_for_row(self, row: dict) -> RoundMenu | None:
        """冲突行的裁决菜单；不是冲突行就没有菜单。"""
        if row["action"] not in CONFLICT_ACTIONS:
            return None
        path = row["path"]
        menu = RoundMenu(parent=self.table)
        menu.addAction(Action("保留本地", triggered=lambda: self._resolve(path, "local")))
        menu.addAction(Action("保留远端", triggered=lambda: self._resolve(path, "remote")))
        menu.addAction(
            Action("已手动合并（推本地）", triggered=lambda: self._resolve(path, "merged"))
        )
        menu.addSeparator()
        menu.addAction(Action("打开对比", triggered=lambda: self._open_diff(path)))
        return menu

    def _sync_buttons(self) -> None:
        self.previewButton.setEnabled(not self._busy)
        # 一个都没勾就别让点「执行」——空清单跑出去只会白等一场
        picked = self._model.checked_paths()
        self.executeButton.setEnabled(
            not self._busy and bool(self._model.fingerprint) and picked != frozenset()
        )
        self._sync_select_all(picked)

    def _sync_select_all(self, picked: frozenset[str] | None) -> None:
        """「全选」跟着实际勾选显示：全勾 → 勾上，一个没勾 → 空，勾了一半 → 半选。"""
        state = (
            Qt.Checked
            if picked is None
            else (Qt.Unchecked if not picked else Qt.PartiallyChecked)
        )
        self.selectAll.blockSignals(True)
        self.selectAll.setCheckState(state)
        self.selectAll.blockSignals(False)

    def _warn(self, title: str, content: str) -> None:
        InfoBar.warning(
            title, content, parent=self, position=InfoBarPosition.TOP_RIGHT, duration=6000
        )
