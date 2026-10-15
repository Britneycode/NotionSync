"""计划页：表格模型、筛选、执行/裁决意图、以及主窗口的闭环接线。

不真起引擎：这里测的是「计划行怎么显示」和「用户动作翻译成什么命令」，
进程那一层在 test_engine_client.py 里单独测。
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from notion_sync_desktop.models.plan_model import (
    COLUMNS,
    PENDING_LABEL,
    ActionFilterProxy,
    PlanTableModel,
)
from notion_sync_desktop.pages import plan_page as mod
from notion_sync_desktop.pages.plan_page import PlanPage, newest_remote_backup
from notion_sync_desktop.services.settings import UiSettings


def make_row(path="a.md", action="PUSH", reason="", local="changed", remote="same"):
    return {
        "path": path,
        "action": action,
        "reason": reason,
        "local": local,
        "remote": remote,
    }


CONFLICT_ROWS = [
    make_row("a.md", "CONFLICT", "L≠BL, R≠BR"),
    make_row("b.md", "BLOCKED", "status=conflict"),
]


class StubEngine(QWidget):
    """只记录被调了什么，不做任何事。"""

    def __init__(self):
        super().__init__()
        self.starts: list[tuple[str, str, tuple[str, ...]]] = []
        self._running = False

    def start(self, command: str, root: str, *flags: str) -> None:
        self.starts.append((command, root, flags))
        self._running = True

    def cancel(self) -> None:
        pass

    def is_running(self) -> bool:
        return self._running


# ------------------------------------------------------------ 表格模型


def test_set_plan_fills_rows_and_fingerprint():
    model = PlanTableModel()
    rows = [make_row("a.md"), make_row("b.md", "SKIP")]

    model.set_plan("fp-1", rows)

    assert model.fingerprint == "fp-1"
    assert model.row_count() == 2
    assert model.row(0)["path"] == "a.md"
    assert model.row(0)["result"] == PENDING_LABEL


def test_set_plan_copies_rows():
    """协议来的 dict 不能跟模型共享——更新结果会污染调用方的数据。"""
    model = PlanTableModel()
    original = make_row()
    model.set_plan("fp", [original])

    model.update_result("a.md", "ok")

    assert "result" not in original


def test_clear_drops_fingerprint():
    model = PlanTableModel()
    model.set_plan("fp", [make_row()])
    model.clear()
    assert model.row_count() == 0
    assert model.fingerprint is None


def test_display_role_translates_side_and_result():
    model = PlanTableModel()
    model.set_plan("fp", [make_row(local="absent", remote="new")])
    model.update_result("a.md", "failed")

    def cell(column, role=Qt.DisplayRole):
        return model.data(model.index(0, column), role)

    assert cell(0) == "PUSH"
    assert cell(1) == "a.md"
    assert cell(2) == "无"
    assert cell(3) == "新建"
    assert cell(4) == "失败"


def test_action_cell_is_colored_and_headers_are_chinese():
    model = PlanTableModel()
    model.set_plan("fp", [make_row(action="CONFLICT", reason="both changed")])

    color = model.data(model.index(0, 0), Qt.ForegroundRole)
    assert color.name() == "#c42b1c"
    # 非动作列不上色
    assert model.data(model.index(0, 1), Qt.ForegroundRole) is None
    assert model.data(model.index(0, 0), Qt.ToolTipRole) == "both changed"
    assert [
        model.headerData(i, Qt.Horizontal, Qt.DisplayRole) for i in range(len(COLUMNS))
    ] == ["动作", "路径", "本地", "远端", "结果"]


def test_update_result_ignores_unknown_path():
    model = PlanTableModel()
    model.set_plan("fp", [make_row()])

    model.update_result("nope.md", "failed")  # 不该抛

    assert model.row(0)["result"] == PENDING_LABEL


def test_update_result_keeps_error_for_tooltip():
    model = PlanTableModel()
    model.set_plan("fp", [make_row()])

    model.update_result("a.md", "failed", "Remote changed during push")

    assert model.data(model.index(0, 0), Qt.ToolTipRole) == "Remote changed during push"


def test_proxy_filters_by_action():
    model = PlanTableModel()
    model.set_plan("fp", [make_row("a.md", "PUSH"), *CONFLICT_ROWS, make_row("s.md", "SKIP")])
    proxy = ActionFilterProxy()
    proxy.setSourceModel(model)

    proxy.set_actions(frozenset({"CONFLICT", "BLOCKED"}))
    assert proxy.rowCount() == 2
    assert proxy.data(proxy.index(0, 1)) == "a.md"  # 排除了 PUSH 与 SKIP

    proxy.set_actions(None)
    assert proxy.rowCount() == 4


# ------------------------------------------------------------ 计划页


@pytest.fixture
def page(tmp_path, qtbot):
    """PlanPage + 隔离的界面偏好文件（绝不碰用户真实的配置目录）。"""
    settings = UiSettings(tmp_path / "settings.json")
    model = PlanTableModel()
    folder = tmp_path / "sync-root"
    folder.mkdir()
    widget = PlanPage(model, lambda: str(folder))
    qtbot.addWidget(widget)
    yield widget, model, folder, settings
    widget.deleteLater()


def test_page_registers_with_the_window_object_name(page):
    widget, _, _, _ = page
    assert widget.objectName() == "planPage"


def test_set_counts_renders_totals(page):
    widget, _, _, _ = page
    widget.set_counts({"PUSH": 3, "CONFLICT": 2})
    assert "共 5 项" in widget.countsLabel.text()
    assert "push=3" in widget.countsLabel.text()
    assert "conflict=2" in widget.countsLabel.text()


def test_set_counts_handles_empty_plan(page):
    widget, _, _, _ = page
    widget.set_counts({})
    assert "计划为空" in widget.countsLabel.text()


def test_default_filter_hides_skip_rows(page):
    widget, model, _, _ = page
    model.set_plan("fp", [make_row("a.md"), make_row("s.md", "SKIP")])
    assert widget.table.model().rowCount() == 1


def test_filter_segments_switch_the_visible_set(page):
    widget, model, _, _ = page
    model.set_plan("fp", [make_row("a.md"), *CONFLICT_ROWS, make_row("s.md", "SKIP")])

    widget.filter.setCurrentItem("冲突")
    assert widget.table.model().rowCount() == 2

    widget.filter.setCurrentItem("无需处理")
    assert widget.table.model().rowCount() == 1

    widget.filter.setCurrentItem("全部")
    assert widget.table.model().rowCount() == 4


def test_execute_without_a_plan_only_warns(page):
    widget, _, _, _ = page
    fired = []
    widget.runRequested.connect(lambda *a: fired.append(a))

    widget.executeButton.click()

    assert fired == []


def test_execute_button_disabled_without_a_plan(page):
    widget, model, _, _ = page
    assert not widget.executeButton.isEnabled()

    model.set_plan("fp-9", [make_row()])
    assert widget.executeButton.isEnabled()


def test_execute_sends_expect_plan(page):
    widget, model, _, _ = page
    model.set_plan("fp-9", [make_row()])
    fired = []
    widget.runRequested.connect(lambda *a: fired.append(a))

    widget.executeButton.click()

    assert fired == [("sync", ["--expect-plan", "fp-9"])]


def test_refresh_preview_asks_for_status(page):
    widget, _, _, _ = page
    fired = []
    widget.runRequested.connect(lambda *a: fired.append(a))

    widget.refresh_preview()

    assert fired == [("status", [])]


def test_buttons_disabled_while_busy(page):
    widget, model, _, _ = page
    model.set_plan("fp", [make_row()])

    widget.set_busy(True)
    assert not widget.previewButton.isEnabled()
    assert not widget.executeButton.isEnabled()

    widget.set_busy(False)
    assert widget.executeButton.isEnabled()


# ------------------------------------------------------------ 右键菜单


def test_no_menu_for_non_conflict_rows(page):
    widget, _, _, _ = page
    assert widget.menu_for_row(make_row(action="PUSH")) is None


def test_conflict_menu_offers_three_verdicts_and_diff(page):
    widget, _, _, _ = page
    menu = widget.menu_for_row(make_row(action="CONFLICT"))

    labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert labels == ["保留本地", "保留远端", "已手动合并（推本地）", "打开对比"]


@pytest.mark.parametrize(
    ("label", "keep"),
    [
        ("保留本地", "local"),
        ("保留远端", "remote"),
        ("已手动合并（推本地）", "merged"),
    ],
)
def test_verdict_actions_send_resolve(page, label, keep):
    widget, _, _, _ = page
    fired = []
    widget.runRequested.connect(lambda *a: fired.append(a))
    menu = widget.menu_for_row(make_row("docs/a.md", "CONFLICT"))

    next(a for a in menu.actions() if a.text() == label).trigger()

    assert fired == [("resolve", ["docs/a.md", "--keep", keep])]


def test_conflict_menu_works_for_blocked_rows(page):
    widget, _, _, _ = page
    assert widget.menu_for_row(make_row(action="BLOCKED")) is not None


# ------------------------------------------------------------ 打开对比


def test_newest_remote_backup_picks_the_latest(tmp_path):
    folder = tmp_path / ".sync-backup" / "conflicts"
    folder.mkdir(parents=True)
    (folder / "a.md.remote-20260101T000000.md").write_text("旧", encoding="utf-8")
    (folder / "a.md.remote-20261015T120000.md").write_text("新", encoding="utf-8")
    (folder / "a.md.remote-20261015T120000.txt").write_text("别的扩展名", encoding="utf-8")

    assert newest_remote_backup(tmp_path, "a.md").name == "a.md.remote-20261015T120000.md"


def test_newest_remote_backup_finds_nested_paths(tmp_path):
    folder = tmp_path / ".sync-backup" / "conflicts" / "docs"
    folder.mkdir(parents=True)
    (folder / "a.md.remote-20260101T000000.md").write_text("x", encoding="utf-8")

    assert newest_remote_backup(tmp_path, "docs/a.md") is not None


def test_newest_remote_backup_returns_none_without_any(tmp_path):
    assert newest_remote_backup(tmp_path, "a.md") is None


def test_open_diff_asks_engine_for_a_remote_copy_first(page):
    widget, _, _, _ = page
    fired = []
    widget.runRequested.connect(lambda *a: fired.append(a))

    menu = widget.menu_for_row(make_row("a.md", "CONFLICT"))
    next(a for a in menu.actions() if a.text() == "打开对比").trigger()

    # 先拿副本，diff 要等引擎跑完（on_run_finished）才拉起来
    assert fired == [("resolve", ["a.md", "--backup-only"])]


def test_open_diff_launches_vscode_when_the_run_succeeds(page, monkeypatch):
    widget, _, folder, _ = page
    backup_dir = folder / ".sync-backup" / "conflicts"
    backup_dir.mkdir(parents=True)
    backup = backup_dir / "a.md.remote-20261015T120000.md"
    backup.write_text("远端", encoding="utf-8")
    (folder / "a.md").write_text("本地", encoding="utf-8")
    calls = []
    monkeypatch.setattr(mod, "open_diff", lambda left, right: calls.append((left, right)))

    widget._open_diff("a.md")
    widget.on_run_finished(0)

    assert calls == [(folder / "a.md", backup)]


def test_open_diff_does_nothing_when_the_run_failed(page, monkeypatch):
    widget, _, _, _ = page
    calls = []
    monkeypatch.setattr(mod, "open_diff", lambda *a: calls.append(a))

    widget._open_diff("a.md")
    widget.on_run_finished(1)

    assert calls == []


def test_open_diff_clears_pending_after_one_run(page, monkeypatch):
    widget, _, folder, _ = page
    backup_dir = folder / ".sync-backup" / "conflicts"
    backup_dir.mkdir(parents=True)
    (backup_dir / "a.md.remote-20261015T120000.md").write_text("远端", encoding="utf-8")
    (folder / "a.md").write_text("本地", encoding="utf-8")
    calls = []
    monkeypatch.setattr(mod, "open_diff", lambda *a: calls.append(a))

    widget._open_diff("a.md")
    widget.on_run_finished(0)
    widget.on_run_finished(0)  # 第二次不该再开一次

    assert len(calls) == 1


def test_open_diff_warns_when_no_backup_landed(page, monkeypatch):
    widget, _, _, _ = page
    calls = []
    monkeypatch.setattr(mod, "open_diff", lambda *a: calls.append(a))

    widget._open_diff("a.md")
    widget.on_run_finished(0)  # 引擎说成功，但磁盘上没有副本

    assert calls == []


def test_open_diff_survives_missing_vscode(page, monkeypatch):
    widget, _, folder, _ = page
    backup_dir = folder / ".sync-backup" / "conflicts"
    backup_dir.mkdir(parents=True)
    (backup_dir / "a.md.remote-20261015T120000.md").write_text("远端", encoding="utf-8")
    (folder / "a.md").write_text("本地", encoding="utf-8")

    def boom(*_a):
        raise mod.CodeNotFound("no code")

    monkeypatch.setattr(mod, "open_diff", boom)

    widget._open_diff("a.md")
    widget.on_run_finished(0)  # 只提示，不该把异常抛到事件循环里


def test_open_diff_rejects_a_second_request_while_pending(page):
    widget, _, _, _ = page
    fired = []
    widget.runRequested.connect(lambda *a: fired.append(a))

    widget._open_diff("a.md")
    widget._open_diff("b.md")

    assert fired == [("resolve", ["a.md", "--backup-only"])]
