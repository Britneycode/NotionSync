"""主窗口接线：协议事件落到哪一页、计划页的动作翻译成什么命令。

这是 P3 的那条闭环——预览 → 执行 → 计划过期自动重预览——唯一一处
把两块拼起来的测试，所以不 mock 事件分发本身，而是发真事件。
"""

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from notion_sync_desktop import main_window as mw
from notion_sync_desktop.services.settings import UiSettings


class StubEngine(QWidget):
    """替代 EngineClient：信号齐备，但不真起子进程。

    `start` 刻意不进「运行中」：真进了的话，pytest-qt 收尾关窗口时
    `MainWindow.closeEvent` 会弹一个模态确认框，没人点它，整个进程就卡死。
    忙碌状态由用例自己发 `runningChanged` 驱动。
    """

    event = Signal(dict)
    diagnostic = Signal(str)
    finished = Signal(int, bool)
    runningChanged = Signal(bool)

    def __init__(self):
        super().__init__()
        self.starts: list[tuple[str, str, tuple[str, ...]]] = []

    def start(self, command: str, root: str, *flags: str) -> None:
        self.starts.append((command, root, flags))

    def cancel(self) -> None:
        pass

    def is_running(self) -> bool:
        return False


def make_folder(tmp_path) -> str:
    folder = tmp_path / "sync-root"
    folder.mkdir(exist_ok=True)
    (folder / ".sync-config.yaml").write_text("include: ['*.md']\n", encoding="utf-8")
    return str(folder)


@pytest.fixture
def window(tmp_path, qtbot, monkeypatch):
    """整个主窗口 + 隔离的界面偏好文件（绝不碰用户真实的配置目录）。"""
    import sync_lib.credentials as creds
    from notion_sync_desktop.pages import sync_page as sync_mod

    settings = UiSettings(tmp_path / "settings.json")
    monkeypatch.setattr(sync_mod, "UiSettings", lambda: settings)
    monkeypatch.setattr(sync_mod, "get_token", lambda: ("secret-token", "env"))
    monkeypatch.setattr(creds, "get_token", lambda: (None, "none"))

    engine = StubEngine()
    monkeypatch.setattr(mw, "EngineClient", lambda parent=None: engine)
    win = mw.MainWindow()
    qtbot.addWidget(win)
    yield win, engine, tmp_path
    win.deleteLater()


PLAN_EVENT = {
    "v": 1,
    "seq": 1,
    "run": "r-test",
    "type": "plan",
    "data": {
        "fingerprint": "fp-1",
        "counts": {"PUSH": 1, "CONFLICT": 1},
        "rows": [
            {
                "path": "a.md",
                "action": "PUSH",
                "reason": "L≠BL, R=BR",
                "local": "changed",
                "remote": "same",
            },
            {
                "path": "b.md",
                "action": "CONFLICT",
                "reason": "both changed",
                "local": "changed",
                "remote": "changed",
            },
        ],
    },
}


def emit(engine, type, **data):
    engine.event.emit({"v": 1, "seq": 1, "run": "r-test", "type": type, "data": data})


# ------------------------------------------------------------ 页面注册


def test_window_registers_all_pages(window):
    win, _, _ = window
    names = [win.stackedWidget.widget(i).objectName() for i in range(win.stackedWidget.count())]
    assert names == ["syncPage", "planPage", "settingsPage"]


# ------------------------------------------------------------ 事件分发


def test_plan_event_fills_model_and_counts(window):
    win, engine, _ = window

    emit(engine, "plan", **PLAN_EVENT["data"])

    assert win.plan_model.fingerprint == "fp-1"
    assert win.plan_model.row_count() == 2
    assert "共 2 项" in win.plan_page.countsLabel.text()


def test_file_finished_updates_the_matching_row(window):
    win, engine, _ = window
    emit(engine, "plan", **PLAN_EVENT["data"])

    emit(engine, "file.finished", path="a.md", action="PUSH", result="ok", error=None)

    assert win.plan_model.row(0)["result"] == "成功"
    assert win.plan_model.row(1)["result"] == "待执行"


def test_run_finished_shows_summary_on_sync_page(window):
    win, engine, _ = window

    emit(engine, "run.finished", exit_code=0, summary={"pushed": 2})

    assert "push=2" in win.sync_page.logView.toPlainText()


# ------------------------------------------------------------ 预览 → 执行


def test_preview_button_switches_to_plan_page_and_runs_status(window):
    win, engine, tmp_path = window
    folder = make_folder(tmp_path)
    win.sync_page.set_folder(folder)

    win.sync_page.previewButton.click()

    assert win.stackedWidget.currentWidget().objectName() == "planPage"
    assert engine.starts == [("status", folder, ())]


def test_execute_from_plan_runs_sync_with_the_fingerprint(window):
    win, engine, tmp_path = window
    folder = make_folder(tmp_path)
    win.sync_page.set_folder(folder)
    emit(engine, "plan", **PLAN_EVENT["data"])

    win.plan_page.executeButton.click()

    assert engine.starts == [("sync", folder, ("--expect-plan", "fp-1"))]


def test_resolve_from_plan_table_runs_resolve(window):
    win, engine, tmp_path = window
    folder = make_folder(tmp_path)
    win.sync_page.set_folder(folder)
    emit(engine, "plan", **PLAN_EVENT["data"])

    menu = win.plan_page.menu_for_row(win.plan_model.row(1))  # b.md / CONFLICT
    next(a for a in menu.actions() if a.text() == "保留远端").trigger()

    assert engine.starts == [("resolve", folder, ("b.md", "--keep", "remote"))]


def test_plan_changed_reprompts_a_preview(window):
    """退出码 76 的那条路：执行被拒 → 自动重新预览。"""
    win, engine, tmp_path = window
    folder = make_folder(tmp_path)
    win.sync_page.set_folder(folder)

    emit(engine, "plan.changed", expected="fp-old", actual="fp-new")

    assert win.stackedWidget.currentWidget().objectName() == "planPage"
    assert engine.starts == [("status", folder, ())]


def test_plan_run_from_the_plan_page_stays_on_it(window):
    """裁决/执行的反馈就在表格那一行上，不该把用户甩回「同步」页。"""
    win, engine, tmp_path = window
    folder = make_folder(tmp_path)
    win.sync_page.set_folder(folder)
    emit(engine, "plan", **PLAN_EVENT["data"])
    win.switchTo(win.plan_page)

    win.plan_page.executeButton.click()

    assert win.stackedWidget.currentWidget().objectName() == "planPage"


def test_busy_state_reaches_the_plan_page(window):
    win, engine, tmp_path = window
    folder = make_folder(tmp_path)
    win.sync_page.set_folder(folder)
    emit(engine, "plan", **PLAN_EVENT["data"])
    assert win.plan_page.executeButton.isEnabled()

    engine.runningChanged.emit(True)

    assert not win.plan_page.executeButton.isEnabled()
    assert not win.sync_page.syncButton.isEnabled()

    engine.runningChanged.emit(False)
    assert win.plan_page.executeButton.isEnabled()