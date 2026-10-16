"""同步页的状态机与按钮使能。

不真起引擎：这里测的是「页面把用户意图翻译成什么命令」，
进程那一层在 test_engine_client.py 里单独测。
"""

import pytest
from PySide6.QtCore import QMimeData, QUrl, Signal
from PySide6.QtWidgets import QWidget

from notion_sync_desktop.pages import sync_page as mod
from notion_sync_desktop.pages.sync_page import EXIT_LABELS, PHASE_LABELS, SyncPage
from notion_sync_desktop.services.settings import UiSettings
from notion_sync_desktop.widgets.folder_drop import first_local_dir


class StubEngine(QWidget):
    """只记录被调了什么，不做任何事。"""

    finished = Signal(int, bool)  # 运行结束（用户命令让路时页面要接上）

    def __init__(self):
        super().__init__()
        self.starts: list[tuple[str, str, tuple[str, ...]]] = []
        self.cancels = 0
        self._running = False

    def start(self, command: str, root: str, *flags: str) -> None:
        self.starts.append((command, root, flags))
        self._running = True

    def cancel(self) -> None:
        self.cancels += 1

    def is_running(self) -> bool:
        return self._running


class StubQuery(QWidget):
    """只读查询（pages）用的引擎替身：能收命令，也能被喂事件。"""

    event = Signal(dict)
    finished = Signal(int, bool)

    def __init__(self):
        super().__init__()
        self.starts: list[tuple[str, str, tuple[str, ...]]] = []

    def start(self, command: str, root: str, *flags: str) -> None:
        self.starts.append((command, root, flags))

    def emit_pages(self, items: list[dict]) -> None:
        self.event.emit({"type": "pages", "data": {"items": items}})
        self.finished.emit(0, False)

    def fail(self) -> None:
        """查询没结果就结束了（没 token / 没网 / 引擎挂了）。"""
        self.finished.emit(3, False)


class _StubButton:
    def setText(self, text: str) -> None:
        self.text = text


class StubMessageBox:
    """确认弹窗替身：按预设答案返回。"""

    def __init__(self, answer: bool = True):
        self._answer = answer
        self.yesButton = _StubButton()
        self.cancelButton = _StubButton()

    def exec(self) -> bool:
        return self._answer


class StubPicker:
    """页面选择器替身：默认选中列表里第一个。"""

    def __init__(self, pages: list[dict], parent=None):
        self.pages = pages
        self._selected = pages[0] if pages else None

    def exec(self) -> bool:
        return self._selected is not None

    def selected_page(self) -> dict | None:
        return self._selected


PARENT_PAGE_ID = "3f9fa72484e880f0a576d312267745ec"


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """SyncPage + 隔离的界面偏好文件（绝不碰用户真实的配置目录）。"""
    settings = UiSettings(tmp_path / "settings.json")
    monkeypatch.setattr(mod, "UiSettings", lambda: settings)
    monkeypatch.setattr(mod, "get_token", lambda: ("secret-token", "env"))
    # 页面查询走的是页面自己起的第二个引擎进程：换成替身，免得测试真起子进程
    monkeypatch.setattr(mod, "EngineClient", lambda parent=None: StubQuery())
    engine = StubEngine()
    page = SyncPage(engine)
    yield page, engine, settings
    page.deleteLater()
    engine.deleteLater()


def make_folder(tmp_path, initialized: bool = True, parent: str = PARENT_PAGE_ID) -> str:
    folder = tmp_path / ("ready" if initialized else "fresh")
    folder.mkdir()
    if initialized:
        (folder / ".sync-config.yaml").write_text(
            f"notion:\n  parent_page_id: {parent}\n", encoding="utf-8"
        )
    return str(folder)


# ------------------------------------------------------------ 同步目标


def test_target_card_hidden_without_a_folder(ui):
    page, _, _ = ui
    page.set_folder("")
    assert not page.targetCard.isVisibleTo(page)


def test_target_card_shows_the_configured_page(ui, tmp_path):
    page, _, _ = ui
    page.set_folder(make_folder(tmp_path))
    assert page.targetCard.isVisibleTo(page)
    # 标题还没查到，先显示 id
    assert page.targetLabel.text() == PARENT_PAGE_ID
    assert page.openTargetLink.isVisibleTo(page)
    assert page.openTargetLink.getUrl().toString() == f"https://www.notion.so/{PARENT_PAGE_ID}"


def test_target_card_says_so_when_uninitialized(ui, tmp_path):
    page, _, _ = ui
    page.set_folder(make_folder(tmp_path, initialized=False))
    assert page.targetCard.isVisibleTo(page)
    assert page.targetLabel.text() == "尚未选择目标页面"
    assert not page.openTargetLink.isVisibleTo(page)


def test_the_title_replaces_the_id_once_the_page_list_arrives(ui, tmp_path):
    page, _, _ = ui
    page.set_folder(make_folder(tmp_path))
    # 已初始化 → 顺带查了一次页面列表
    assert page._query is not None
    assert page._query.starts[0][0] == "pages"

    page._query.emit_pages([{"id": PARENT_PAGE_ID, "title": "工作台", "url": "u"}])

    assert page.targetLabel.text() == "工作台"


def test_a_failed_page_query_keeps_the_id(ui, tmp_path):
    """查标题只是锦上添花：查不到就继续显示 id，不该弹警告打断用户。"""
    page, _, _ = ui
    page.set_folder(make_folder(tmp_path))

    page._query.fail()

    assert page.targetLabel.text() == PARENT_PAGE_ID


def test_no_token_means_no_page_query(ui, tmp_path, monkeypatch):
    page, _, _ = ui
    monkeypatch.setattr(mod, "get_token", lambda: (None, "none"))
    page.set_folder(make_folder(tmp_path))
    assert page._query is None
    assert page.targetLabel.text() == PARENT_PAGE_ID


# ---------------------------------------------------------------- 启动前拦


def test_run_without_folder_starts_nothing(ui):
    page, engine, _ = ui
    page.run("sync")
    assert engine.starts == []


def test_run_on_missing_folder_starts_nothing(ui, tmp_path):
    page, engine, _ = ui
    page.set_folder(str(tmp_path / "not-there"))
    page.run("sync")
    assert engine.starts == []


def test_run_on_uninitialized_folder_starts_nothing(ui, tmp_path):
    page, engine, _ = ui
    page.set_folder(make_folder(tmp_path, initialized=False))
    page.run("sync")
    assert engine.starts == []
    assert page.targetCard.isVisibleTo(page)


def test_run_without_token_asks_for_settings(ui, tmp_path, monkeypatch):
    page, engine, _ = ui
    monkeypatch.setattr(mod, "get_token", lambda: (None, "none"))
    asked = []
    page.openSettingsRequested.connect(lambda: asked.append(True))
    page.set_folder(make_folder(tmp_path))
    page.run("sync")
    assert engine.starts == []
    assert asked == [True]


# ------------------------------------------------------------ 选择目标页面


def test_choosing_a_page_runs_init(ui, tmp_path, monkeypatch):
    """未初始化：选中即建库，不用再确认。"""
    page, engine, _ = ui
    folder = make_folder(tmp_path, initialized=False)
    page.set_folder(folder)
    page._pages = [{"id": "abc", "title": "工作台", "url": "u"}]
    monkeypatch.setattr(mod, "PagePickerDialog", StubPicker)

    page.choosePageButton.click()

    assert engine.starts == [("init", folder, ("--parent-page-id", "abc"))]


def test_reinit_asks_for_confirmation_first(ui, tmp_path, monkeypatch):
    """已初始化：先问清楚，用户点了「算了」就什么都不做。"""
    page, engine, _ = ui
    folder = make_folder(tmp_path)
    page.set_folder(folder)
    page._pages = [{"id": "abc", "title": "工作台", "url": "u"}]
    monkeypatch.setattr(mod, "PagePickerDialog", StubPicker)
    monkeypatch.setattr(mod, "MessageBox", lambda *a, **k: StubMessageBox(answer=False))

    page.choosePageButton.click()

    assert engine.starts == []


def test_reinit_goes_through_after_confirmation(ui, tmp_path, monkeypatch):
    page, engine, _ = ui
    folder = make_folder(tmp_path)
    page.set_folder(folder)
    page._pages = [{"id": "abc", "title": "工作台", "url": "u"}]
    monkeypatch.setattr(mod, "PagePickerDialog", StubPicker)
    monkeypatch.setattr(mod, "MessageBox", lambda *a, **k: StubMessageBox(answer=True))

    page.choosePageButton.click()

    assert engine.starts == [("init", folder, ("--parent-page-id", "abc"))]


def test_an_empty_page_list_starts_nothing(ui, tmp_path, monkeypatch):
    """integration 没连到任何页面：选择器里没得选，也就发不出命令。"""
    page, engine, _ = ui
    page.set_folder(make_folder(tmp_path, initialized=False))
    page._pages = []
    monkeypatch.setattr(mod, "PagePickerDialog", StubPicker)

    page.choosePageButton.click()

    assert engine.starts == []


def test_choose_page_without_a_folder_warns(ui, monkeypatch):
    page, engine, _ = ui
    monkeypatch.setattr(mod, "PagePickerDialog", StubPicker)
    page.choosePageButton.click()
    assert engine.starts == []


def test_choose_page_waits_for_the_list_then_opens_the_picker(ui, tmp_path, monkeypatch):
    """列表还没查到：先按「读取页面…」挡住，查完自动弹出来。"""
    page, engine, _ = ui
    folder = make_folder(tmp_path)
    page.set_folder(folder)
    opened: list[list] = []
    monkeypatch.setattr(
        mod, "PagePickerDialog", lambda pages, parent=None: opened.append(pages) or StubPicker(pages)
    )
    # 选中之后还会走「换页面」的确认：离屏下真弹模态框会一直等下去
    monkeypatch.setattr(mod, "MessageBox", lambda *a, **k: StubMessageBox(answer=True))

    page.choosePageButton.click()          # 起查询，此时还没列表
    assert opened == []
    assert page.choosePageButton.text() == "读取页面…"

    page._query.emit_pages([{"id": "abc", "title": "工作台", "url": "u"}])

    assert opened == [[{"id": "abc", "title": "工作台", "url": "u"}]]
    assert page.choosePageButton.text() == "选择页面…"
    assert engine.starts == [("init", folder, ("--parent-page-id", "abc"))]


# ------------------------------------------------------------ 命令与参数


@pytest.mark.parametrize(
    ("button", "command", "flags"),
    [
        ("syncButton", "sync", ()),
        ("pushButton", "push", ()),
        ("pullButton", "pull", ()),
    ],
)
def test_buttons_map_to_commands(ui, tmp_path, button, command, flags):
    page, engine, _ = ui
    folder = make_folder(tmp_path)
    page.set_folder(folder)
    getattr(page, button).click()
    assert engine.starts == [(command, folder, flags)]


def test_preview_button_asks_for_the_plan_page(ui, tmp_path):
    """「预览计划」不再自己跑命令，交给计划页（P3 的预览→执行闭环）。"""
    page, engine, _ = ui
    page.set_folder(make_folder(tmp_path))
    fired = []
    page.previewRequested.connect(lambda: fired.append(True))

    page.previewButton.click()

    assert fired == [True]
    assert engine.starts == []


def test_run_remembers_folder_and_clears_log(ui, tmp_path):
    page, engine, settings = ui
    folder = make_folder(tmp_path)
    page.set_folder(folder)
    page.append_log("info", "上一轮的日志")
    page.run("sync")
    assert settings.last_folder == folder
    assert "上一轮的日志" not in page.logView.toPlainText()
    assert folder in page.logView.toPlainText()


def test_last_folder_survives_restart(tmp_path, monkeypatch):
    settings = UiSettings(tmp_path / "settings.json")
    monkeypatch.setattr(mod, "UiSettings", lambda: settings)
    # 这个用例自己造页面，不走 ui fixture：查询引擎与 token 也得自己挡住
    monkeypatch.setattr(mod, "EngineClient", lambda parent=None: StubQuery())
    monkeypatch.setattr(mod, "get_token", lambda: ("secret-token", "env"))
    folder = make_folder(tmp_path)
    engine = StubEngine()
    page = SyncPage(engine)
    page.set_folder(folder)
    page.run("sync")
    page.deleteLater()

    fresh = SyncPage(StubEngine())
    assert fresh.folder() == folder
    fresh.deleteLater()
    engine.deleteLater()


# ------------------------------------------------------------ 忙碌状态


def test_set_busy_disables_actions_and_shows_cancel(ui):
    page, _, _ = ui
    page.set_busy(True)
    for button in (page.syncButton, page.pushButton, page.pullButton, page.previewButton):
        assert not button.isEnabled()
    assert not page.choosePageButton.isEnabled()
    assert page.cancelButton.isVisibleTo(page)
    # 还不知道总数：进度条先转起来
    assert page.progressBar.maximum() == 0


def test_set_busy_false_restores(ui):
    page, _, _ = ui
    page.set_busy(True)
    page.set_busy(False)
    for button in (page.syncButton, page.pushButton, page.pullButton, page.previewButton):
        assert button.isEnabled()
    assert not page.cancelButton.isVisibleTo(page)
    assert page.progressBar.value() == 1


def test_cancel_button_asks_engine_to_cancel(ui):
    page, engine, _ = ui
    page.set_busy(True)
    page.cancelButton.click()
    assert engine.cancels == 1


# ------------------------------------------------------------ 事件渲染


def test_set_phase_translates_known_names(ui):
    page, _, _ = ui
    page.set_phase("scan")
    assert page.phaseLabel.text() == f"阶段：{PHASE_LABELS['scan']}"


def test_set_phase_passes_unknown_names_through(ui):
    page, _, _ = ui
    page.set_phase("new-phase")
    assert page.phaseLabel.text() == "阶段：new-phase"


def test_set_progress_and_current(ui):
    page, _, _ = ui
    page.set_progress(3, 7)
    assert (page.progressBar.value(), page.progressBar.maximum()) == (3, 7)
    page.set_current("docs/a.md", 3, 7)
    assert page.fileLabel.text() == "[3/7] docs/a.md"


def test_file_result_lines(ui):
    page, _, _ = ui
    page.append_file_result({"path": "a.md", "result": "ok"})
    page.append_file_result({"path": "b.md", "result": "failed", "error": "boom"})
    log = page.logView.toPlainText()
    assert "✓ a.md" in log
    assert "[错误] ✗ b.md — boom" in log


def test_diagnostic_is_prefixed_per_line(ui):
    page, _, _ = ui
    page.append_diagnostic("第一行\n\n第二行\n")
    log = page.logView.toPlainText()
    assert "[引擎] 第一行" in log
    assert "[引擎] 第二行" in log
    assert log.count("[引擎]") == 2


def test_show_summary_uses_exit_label(ui):
    page, _, _ = ui
    page.show_summary({"pushed": 2, "failed": 1}, 1)
    assert page.phaseLabel.text() == f"阶段：{EXIT_LABELS[1]}"
    assert "push=2" in page.logView.toPlainText()
    assert "errors=1" in page.logView.toPlainText()


def test_summary_text_handles_empty_summary(ui):
    page, _, _ = ui
    assert "push=0" in page.summary_text({})
    assert "errors=0" in page.summary_text({})


def test_run_finished_refreshes_the_target(ui, tmp_path):
    """init 成功后「同步到」要换成新页面。"""
    page, _, _ = ui
    folder = make_folder(tmp_path, initialized=False)
    page.set_folder(folder)
    assert page.targetLabel.text() == "尚未选择目标页面"
    (tmp_path / "fresh" / ".sync-config.yaml").write_text(
        f"notion:\n  parent_page_id: {PARENT_PAGE_ID}\n", encoding="utf-8"
    )
    page.on_run_finished(0)
    assert page.targetLabel.text() == PARENT_PAGE_ID


# ------------------------------------------------------------ 拖拽落点


def test_first_local_dir_picks_the_folder(tmp_path):
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    assert first_local_dir(md) == tmp_path


def test_first_local_dir_ignores_files_and_remote_urls(tmp_path):
    a_file = tmp_path / "a.md"
    a_file.write_text("x", encoding="utf-8")
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(a_file)), QUrl("https://example.com")])
    assert first_local_dir(md) is None


def test_first_local_dir_without_urls():
    assert first_local_dir(QMimeData()) is None
