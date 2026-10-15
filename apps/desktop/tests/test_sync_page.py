"""同步页的状态机与按钮使能。

不真起引擎：这里测的是「页面把用户意图翻译成什么命令」，
进程那一层在 test_engine_client.py 里单独测。
"""

import pytest
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QWidget

from notion_sync_desktop.pages import sync_page as mod
from notion_sync_desktop.pages.sync_page import EXIT_LABELS, PHASE_LABELS, SyncPage
from notion_sync_desktop.services.settings import UiSettings
from notion_sync_desktop.widgets.folder_drop import first_local_dir


class StubEngine(QWidget):
    """只记录被调了什么，不做任何事。"""

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


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """SyncPage + 隔离的界面偏好文件（绝不碰用户真实的配置目录）。"""
    settings = UiSettings(tmp_path / "settings.json")
    monkeypatch.setattr(mod, "UiSettings", lambda: settings)
    monkeypatch.setattr(mod, "get_token", lambda: ("secret-token", "env"))
    engine = StubEngine()
    page = SyncPage(engine)
    yield page, engine, settings
    page.deleteLater()
    engine.deleteLater()


def make_folder(tmp_path, initialized: bool = True) -> str:
    folder = tmp_path / ("ready" if initialized else "fresh")
    folder.mkdir()
    if initialized:
        (folder / ".sync-config.yaml").write_text("root_page_id: x\n", encoding="utf-8")
    return str(folder)


# ------------------------------------------------------------ 初始化卡片


def test_init_card_hidden_for_initialized_folder(ui, tmp_path):
    page, _, _ = ui
    page.set_folder(make_folder(tmp_path, initialized=True))
    assert not page.initCard.isVisibleTo(page)


def test_init_card_shown_for_uninitialized_folder(ui, tmp_path):
    page, _, _ = ui
    page.set_folder(make_folder(tmp_path, initialized=False))
    assert page.initCard.isVisibleTo(page)


def test_init_card_hidden_when_no_folder(ui):
    page, _, _ = ui
    page.set_folder("")
    assert not page.initCard.isVisibleTo(page)


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
    assert page.initCard.isVisibleTo(page)


def test_run_without_token_asks_for_settings(ui, tmp_path, monkeypatch):
    page, engine, _ = ui
    monkeypatch.setattr(mod, "get_token", lambda: (None, "none"))
    asked = []
    page.openSettingsRequested.connect(lambda: asked.append(True))
    page.set_folder(make_folder(tmp_path))
    page.run("sync")
    assert engine.starts == []
    assert asked == [True]


def test_init_is_allowed_before_initialization(ui, tmp_path):
    """init 就是用来建配置的，不能被「还没初始化」拦住。"""
    page, engine, _ = ui
    folder = make_folder(tmp_path, initialized=False)
    page.set_folder(folder)
    page.parentPageEdit.setText("0123456789abcdef0123456789abcdef")
    page.initButton.click()
    assert engine.starts == [("init", folder, ("--parent-page-id", "0123456789abcdef0123456789abcdef"))]


def test_init_without_parent_page_warns(ui, tmp_path):
    page, engine, _ = ui
    page.set_folder(make_folder(tmp_path, initialized=False))
    page.initButton.click()
    assert engine.starts == []


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
    assert not page.initButton.isEnabled()
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


def test_run_finished_refreshes_init_card(ui, tmp_path):
    """init 成功后「初始化」卡片要收起来。"""
    page, _, _ = ui
    folder = make_folder(tmp_path, initialized=False)
    page.set_folder(folder)
    assert page.initCard.isVisibleTo(page)
    (tmp_path / "fresh" / ".sync-config.yaml").write_text("x: 1\n", encoding="utf-8")
    page.on_run_finished(0)
    assert not page.initCard.isVisibleTo(page)


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
