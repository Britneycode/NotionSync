"""设置页：主题下拉与书签列表。

`setTheme` 被换成一个记录器——真去改全局主题会污染同一进程里后面的用例。
"""

import pytest

from notion_sync_desktop.pages import settings_page as mod
from notion_sync_desktop.services.settings import UiSettings


@pytest.fixture
def page(tmp_path, qtbot, monkeypatch):
    settings = UiSettings(tmp_path / "settings.json")
    monkeypatch.setattr(mod, "UiSettings", lambda: settings)
    monkeypatch.setattr(mod.credentials, "get_token", lambda: (None, "none"))
    applied: list[str] = []
    monkeypatch.setattr(mod, "apply_theme", lambda theme: applied.append(theme))
    folder = tmp_path / "ready"
    folder.mkdir()
    widget = mod.SettingsPage(lambda: str(folder))
    qtbot.addWidget(widget)
    yield widget, settings, applied, folder
    widget.deleteLater()


# ---------------------------------------------------------------- 主题


def test_default_theme_follows_the_system(page):
    widget, _settings, applied, _folder = page
    assert widget.themeCombo.currentText() == "跟随系统"
    assert applied == []  # 只是显示当前值，不该顺手写一次盘


def test_theme_change_is_saved_and_applied(page, tmp_path):
    widget, settings, applied, _folder = page

    widget.themeCombo.setCurrentIndex(2)  # 深色

    assert settings.theme == "dark"
    assert applied == ["dark"]
    # 落盘了：换一个实例读回来还是它
    assert UiSettings(tmp_path / "settings.json").theme == "dark"


def test_saved_theme_shows_up_next_time(tmp_path, qtbot, monkeypatch):
    path = tmp_path / "settings.json"
    UiSettings(path).theme = "light"
    monkeypatch.setattr(mod, "UiSettings", lambda: UiSettings(path))
    monkeypatch.setattr(mod.credentials, "get_token", lambda: (None, "none"))
    widget = mod.SettingsPage(lambda: "")
    qtbot.addWidget(widget)

    assert widget.themeCombo.currentIndex() == 1
    widget.deleteLater()


# ---------------------------------------------------------------- 书签


def test_add_bookmark_takes_the_current_folder(page):
    widget, settings, _applied, folder = page

    widget.addBookmarkButton.click()

    assert settings.bookmarks == [str(folder)]
    assert widget.bookmarkList.count() == 1
    assert widget.bookmarkList.item(0).text() == str(folder)


def test_add_bookmark_is_idempotent(page):
    widget, settings, _applied, folder = page

    widget.addBookmarkButton.click()
    widget.addBookmarkButton.click()

    assert settings.bookmarks == [str(folder)]
    assert widget.bookmarkList.count() == 1


def test_add_bookmark_without_a_folder_adds_nothing(tmp_path, qtbot, monkeypatch):
    settings = UiSettings(tmp_path / "settings.json")
    monkeypatch.setattr(mod, "UiSettings", lambda: settings)
    monkeypatch.setattr(mod.credentials, "get_token", lambda: (None, "none"))
    widget = mod.SettingsPage(lambda: "")
    qtbot.addWidget(widget)

    widget.addBookmarkButton.click()

    assert settings.bookmarks == []
    widget.deleteLater()


def test_remove_selected_bookmark(page):
    widget, settings, _applied, folder = page
    widget.addBookmarkButton.click()
    widget.bookmarkList.setCurrentRow(0)

    widget.removeBookmarkButton.click()

    assert settings.bookmarks == []
    assert widget.bookmarkList.count() == 0


def test_remove_button_needs_a_selection(page):
    widget, _settings, _applied, _folder = page
    assert not widget.removeBookmarkButton.isEnabled()

    widget.addBookmarkButton.click()
    widget.bookmarkList.setCurrentRow(0)

    assert widget.removeBookmarkButton.isEnabled()


def test_double_click_asks_to_open_that_folder(page, qtbot):
    widget, _settings, _applied, folder = page
    widget.addBookmarkButton.click()

    with qtbot.waitSignal(widget.openFolderRequested) as blocker:
        widget.bookmarkList.itemDoubleClicked.emit(widget.bookmarkList.item(0))

    assert blocker.args == [str(folder)]


def test_missing_folders_are_hidden_but_kept_on_disk(page):
    """文件夹被删/移走：列表里看不到，但文件里还留着，回来就还在。"""
    widget, settings, _applied, folder = page
    widget.addBookmarkButton.click()

    folder.rename(folder.with_name("moved"))
    widget.refresh_bookmarks()

    assert widget.bookmarkList.count() == 0
    folder.with_name("moved").rename(folder)
    widget.refresh_bookmarks()
    assert widget.bookmarkList.count() == 1


# ---------------------------------------------------------------- 开机自启


class StubAutostart:
    """替代 services.autostart：记录被调了什么，不真碰注册表。"""

    def __init__(self, enabled: bool = False, fail: bool = False):
        self.enabled = enabled
        self.fail = fail
        self.calls: list[bool] = []

    def is_enabled(self) -> bool:
        return self.enabled

    def enable(self) -> None:
        self.calls.append(True)
        if self.fail:
            raise OSError("拒绝访问")

    def disable(self) -> None:
        self.calls.append(False)
        if self.fail:
            raise OSError("拒绝访问")


@pytest.fixture
def startup_page(tmp_path, qtbot, monkeypatch):
    """带自启开关的设置页；注册表读写整个换成桩。"""
    stub = StubAutostart()
    monkeypatch.setattr(mod, "autostart", stub)
    monkeypatch.setattr(mod, "UiSettings", lambda: UiSettings(tmp_path / "settings.json"))
    monkeypatch.setattr(mod.credentials, "get_token", lambda: (None, "none"))
    widget = mod.SettingsPage(lambda: "")
    qtbot.addWidget(widget)
    yield widget, stub
    widget.deleteLater()


def test_autostart_switch_reads_the_state_on_build(tmp_path, qtbot, monkeypatch):
    """开关一开始就反映注册表里的现状，且这一步不该反过来去写注册表。"""
    stub = StubAutostart(enabled=True)
    monkeypatch.setattr(mod, "autostart", stub)
    monkeypatch.setattr(mod, "UiSettings", lambda: UiSettings(tmp_path / "settings.json"))
    monkeypatch.setattr(mod.credentials, "get_token", lambda: (None, "none"))
    widget = mod.SettingsPage(lambda: "")
    qtbot.addWidget(widget)

    assert widget.autostartSwitch.isChecked()
    assert stub.calls == []
    widget.deleteLater()


def test_turning_it_on_writes_the_registry_value(startup_page):
    widget, stub = startup_page

    widget.autostartSwitch.setChecked(True)

    assert stub.calls == [True]


def test_turning_it_off_removes_the_registry_value(startup_page):
    widget, stub = startup_page
    widget.autostartSwitch.setChecked(True)

    widget.autostartSwitch.setChecked(False)

    assert stub.calls == [True, False]


def test_a_failed_write_bounces_the_switch_back(startup_page):
    widget, stub = startup_page
    stub.fail = True

    widget.autostartSwitch.setChecked(True)

    assert widget.autostartSwitch.isChecked() is False
    assert stub.calls == [True]  # 弹回去不该再写一次注册表
