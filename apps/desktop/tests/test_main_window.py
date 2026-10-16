"""主窗口接线：协议事件落到哪一页、计划页的动作翻译成什么命令、托盘怎么反应。

这是 P3/P4 那几条闭环——预览 → 执行 → 计划过期自动重预览、运行 → 历史 ——唯一
一处把几块拼起来的测试，所以不 mock 事件分发本身，而是发真事件。
"""

import json
from pathlib import Path

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QSystemTrayIcon, QWidget

from notion_sync_desktop import main_window as mw
from notion_sync_desktop.services.app_icon import app_icon
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
        self.cancels = 0

    def start(self, command: str, root: str, *flags: str) -> None:
        self.starts.append((command, root, flags))

    def cancel(self) -> None:
        self.cancels += 1

    def is_running(self) -> bool:
        return False


class StubQueryEngine(QWidget):
    """同步页的只读查询（pages）引擎替身：收下命令就完了，绝不真起子进程。"""

    event = Signal(dict)
    finished = Signal(int, bool)

    def start(self, command: str, root: str, *flags: str) -> None:
        self.last_start = (command, root, flags)


class StubMessageBox:
    def __init__(self, answer: bool):
        self._answer = answer
        self.shown: list[str] = []

    def exec(self) -> bool:
        return self._answer


class StubApplication:
    """只记录 `quit()`；真 quit 会把 pytest-qt 的事件循环干掉。"""

    quits = 0

    @staticmethod
    def quit() -> None:
        StubApplication.quits += 1


def make_folder(tmp_path, name: str = "sync-root") -> str:
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    (folder / ".sync-config.yaml").write_text("include: ['*.md']\n", encoding="utf-8")
    return str(folder)


def build_window(monkeypatch, tmp_path, tray: bool = False, runs=None):
    """整个主窗口 + 隔离的界面偏好与历史目录（绝不碰用户真实的配置目录）。"""
    import sync_lib.credentials as creds
    from notion_sync_desktop.pages import settings_page as settings_mod
    from notion_sync_desktop.pages import sync_page as sync_mod
    from notion_sync_desktop.services import history

    settings = UiSettings(tmp_path / "settings.json")
    monkeypatch.setattr(sync_mod, "UiSettings", lambda: settings)
    monkeypatch.setattr(settings_mod, "UiSettings", lambda: settings)
    monkeypatch.setattr(sync_mod, "get_token", lambda: ("secret-token", "env"))
    monkeypatch.setattr(creds, "get_token", lambda: (None, "none"))
    monkeypatch.setattr(history, "runs_dir", lambda: runs or tmp_path / "runs")
    # 离屏环境本来就没有系统托盘，这里显式挑分支（见 main_window.tray_available）
    monkeypatch.setattr(mw, "tray_available", lambda: tray)

    engine = StubEngine()
    monkeypatch.setattr(mw, "EngineClient", lambda parent=None: engine)
    # 同步页查目标页面标题时自己会再起一个引擎进程，也挡住
    monkeypatch.setattr(sync_mod, "EngineClient", lambda parent=None: StubQueryEngine())
    return mw.MainWindow(), engine, tmp_path


@pytest.fixture
def window(tmp_path, qtbot, monkeypatch):
    win, engine, root = build_window(monkeypatch, tmp_path)
    qtbot.addWidget(win)
    yield win, engine, root
    win.deleteLater()


@pytest.fixture
def tray_window(tmp_path, qtbot, monkeypatch):
    win, engine, root = build_window(monkeypatch, tmp_path, tray=True)
    qtbot.addWidget(win)
    yield win, engine, root
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
    assert names == ["syncPage", "planPage", "historyPage", "settingsPage"]


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


# ------------------------------------------------------------ 书签换文件夹


def test_bookmark_opens_that_folder_on_the_sync_page(window):
    win, _, tmp_path = window
    folder = make_folder(tmp_path, "other")

    win.settings_page.openFolderRequested.emit(folder)

    assert win.sync_page.folder() == folder
    assert win.stackedWidget.currentWidget().objectName() == "syncPage"


# ------------------------------------------------------------ 运行历史


def test_a_run_lands_in_the_history_directory(window):
    win, engine, tmp_path = window

    emit(engine, "run.started", command="sync", root=str(tmp_path))
    engine.finished.emit(0, False)

    assert win.history.path is not None
    assert win.history.path.parent == tmp_path / "runs"
    assert win.history.path.name.endswith("-sync.jsonl")
    lines = [json.loads(x) for x in win.history.path.read_text(encoding="utf-8").splitlines()]
    assert lines[-1] == {"type": "app.exit", "data": {"exit_code": 0, "crashed": False}}


def test_finished_run_shows_up_on_the_history_page(window):
    """跑完一次，最近运行列表里立刻能点到它——`_on_finished` 里 refresh 的那一下。"""
    win, engine, tmp_path = window

    emit(engine, "run.started", command="sync", root=str(tmp_path))
    emit(engine, "plan", **PLAN_EVENT["data"])
    emit(engine, "file.finished", path="a.md", action="PUSH", result="ok", error=None)
    emit(engine, "run.finished", exit_code=0, summary={"pushed": 1})
    engine.finished.emit(0, False)

    page = win.history_page
    assert page.model.run_count() == 1
    page.table.selectRow(0)
    page.replay(page.model.run(0))
    assert page.plan_model.row_count() == 2
    assert page.plan_model.row(0)["result"] == "成功"


def test_unwritable_history_directory_is_reported_in_the_log(tmp_path, qtbot, monkeypatch):
    """历史是附属品：写不进去要说一声，但同步照跑。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("我不是目录", encoding="utf-8")

    win, engine, _ = build_window(monkeypatch, tmp_path, runs=blocked)
    qtbot.addWidget(win)

    emit(engine, "run.started", command="sync", root=str(tmp_path))

    assert win.history.path is None
    assert "没能写进历史" in win.sync_page.logView.toPlainText()
    win.deleteLater()


# ---------------------------------------------------------------- 托盘


def test_no_tray_means_closing_really_closes(window):
    win, _, _ = window
    win.show()
    assert win.tray is None

    assert win.close() is True
    assert not win.isVisible()


def test_tray_resident_window_keeps_running(tray_window):
    """有托盘：关窗口只是收起来，进程和托盘图标都还在。"""
    win, _, _ = tray_window
    win.show()
    assert win.tray is not None

    win.close()

    assert not win.isVisible()
    assert win.isHidden()  # 藏起来，不是销毁
    assert win.tray.isVisible()

    win.show_window()
    assert win.isVisible()


def test_tray_shows_the_app_icon(tray_window):
    """托盘是程序在桌面上的常驻面孔，得跟窗口图标一致，而不是 Fluent 的通用字形。"""
    win, _, _ = tray_window

    assert win.tray.icon().pixmap(32, 32).toImage() == app_icon().pixmap(32, 32).toImage()


def test_tray_menu_offers_sync_window_and_quit(tray_window):
    win, _, _ = tray_window

    labels = [a.text() for a in win.tray.contextMenu().actions()]

    assert labels == ["同步当前文件夹", "打开窗口", "退出"]


def test_tray_sync_action_runs_sync_on_the_current_folder(tray_window):
    win, engine, tmp_path = tray_window
    folder = make_folder(tmp_path)
    win.sync_page.set_folder(folder)

    next(
        a for a in win.tray.contextMenu().actions() if a.text() == "同步当前文件夹"
    ).trigger()

    assert engine.starts == [("sync", folder, ())]


def test_tray_click_brings_the_window_back(tray_window):
    win, _, _ = tray_window
    win.hide()

    win.tray.activated.emit(QSystemTrayIcon.Trigger)

    assert win.isVisible()


def test_run_finished_sends_a_tray_notification(tray_window, monkeypatch):
    win, engine, _ = tray_window
    messages: list[tuple] = []
    monkeypatch.setattr(win.tray, "showMessage", lambda *a: messages.append(a))

    emit(engine, "run.finished", exit_code=2, summary={"pushed": 1, "conflicts": 2})

    assert messages
    assert "有冲突" in messages[0][0]
    assert "push=1" in messages[0][1]


def test_quit_while_idle_closes_the_application(tray_window, monkeypatch):
    win, _, _ = tray_window
    monkeypatch.setattr(mw, "QApplication", StubApplication)
    StubApplication.quits = 0

    win.quit()

    assert StubApplication.quits == 1


def test_quit_while_running_cancels_first(tray_window, monkeypatch):
    win, engine, _ = tray_window
    monkeypatch.setattr(mw, "QApplication", StubApplication)
    monkeypatch.setattr(mw, "MessageBox", lambda *a, **k: StubMessageBox(True))
    monkeypatch.setattr(engine, "is_running", lambda: True)
    StubApplication.quits = 0

    win.quit()

    assert engine.cancels == 1
    assert StubApplication.quits == 0  # 等进程退干净再退
    engine.finished.emit(130, False)
    assert StubApplication.quits == 1


def test_declining_the_quit_confirmation_keeps_running(tray_window, monkeypatch):
    win, engine, _ = tray_window
    monkeypatch.setattr(mw, "QApplication", StubApplication)
    monkeypatch.setattr(mw, "MessageBox", lambda *a, **k: StubMessageBox(False))
    monkeypatch.setattr(engine, "is_running", lambda: True)
    StubApplication.quits = 0

    win.quit()

    assert engine.cancels == 0
    assert StubApplication.quits == 0
    # 取消之后关窗口还得回到「收进托盘」那条路，而不是当成正在退出
    win.show()
    win.close()
    assert win.isHidden()


# ------------------------------------------------------- 文件监听（2.3）


def _init_folder(root):
    """造一个「已初始化」的同步文件夹：配置 + 状态。"""
    folder = root / "project"
    folder.mkdir()
    (folder / ".sync-config.yaml").write_text(
        "include: ['*.md']\nexclude: []\n", encoding="utf-8"
    )
    (folder / ".sync-state.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "files": {},
                "tombstones": {},
                "notion": {"database_id": "db", "data_source_id": "ds"},
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_folder_change_restarts_the_watcher(window, qtbot):
    """换了文件夹，监听跟着换：新文件夹里的改动会自动触发一次增量同步。

    启动时选的那个文件夹和后来拖进来的必须一视同仁，否则「自动同步」只在
    重启后才对新文件夹生效。
    """
    win, engine, root = window
    folder = _init_folder(root)

    win.sync_page.set_folder(str(folder))
    (folder / "note.md").write_text("hello", encoding="utf-8")

    qtbot.waitUntil(lambda: bool(engine.starts), timeout=8000)
    command, _root, flags = engine.starts[0]
    assert command == "sync"
    assert "--only-file" in flags
    assert "note.md" in Path(flags[flags.index("--only-file") + 1]).read_text(encoding="utf-8")


def test_watch_toggle_stops_and_starts_the_watcher(window, qtbot):
    win, engine, root = window
    folder = _init_folder(root)
    win.sync_page.set_folder(str(folder))

    win.settings_page.watchSwitch.setChecked(False)
    (folder / "a.md").write_text("x", encoding="utf-8")
    qtbot.wait(400)
    assert engine.starts == []

    win.settings_page.watchSwitch.setChecked(True)
    (folder / "b.md").write_text("y", encoding="utf-8")
    qtbot.waitUntil(lambda: bool(engine.starts), timeout=8000)
