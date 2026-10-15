"""开机自启：`HKCU\\...\\Run` 里那条值的读写。

这几条**真写注册表**——但值名换成一次性的 uuid，测完删掉，绝不动 `NotionSync` 那条
真值（那是用户自己的开机自启）。注册表没有"临时目录"可用，只能这么隔离。
"""

import uuid
import winreg

import pytest

from notion_sync_desktop.services import autostart


@pytest.fixture
def temp_value(monkeypatch):
    monkeypatch.setattr(autostart, "VALUE_NAME", f"NotionSync-test-{uuid.uuid4().hex}")
    yield autostart.VALUE_NAME
    autostart.disable()


def write_raw(name: str, command: str) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)


# ---------------------------------------------------------------- 命令行


def test_launcher_runs_the_module_in_dev(monkeypatch):
    monkeypatch.delattr(autostart.sys, "frozen", raising=False)

    assert autostart.launcher() == f'"{autostart.sys.executable}" -m notion_sync_desktop --tray'


def test_launcher_runs_the_exe_when_frozen(monkeypatch):
    monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)

    assert autostart.launcher() == f'"{autostart.sys.executable}" --tray'


def test_exe_is_taken_from_the_quotes():
    assert autostart._exe_of('"C:\\a b\\NotionSync.exe" --tray') == "C:\\a b\\NotionSync.exe"


def test_exe_is_taken_without_quotes():
    assert autostart._exe_of("C:\\x\\NotionSync.exe --tray") == "C:\\x\\NotionSync.exe"


# ---------------------------------------------------------------- 注册表


def test_nothing_registered_means_off(temp_value):
    assert autostart.registered() == ""
    assert autostart.is_enabled() is False


def test_enable_writes_the_launcher(temp_value):
    autostart.enable()

    assert autostart.registered() == autostart.launcher()
    assert autostart.is_enabled() is True


def test_disable_removes_it(temp_value):
    autostart.enable()

    autostart.disable()

    assert autostart.registered() == ""
    assert autostart.is_enabled() is False


def test_disable_without_the_value_is_a_no_op(temp_value):
    autostart.disable()
    autostart.disable()

    assert autostart.registered() == ""


def test_a_value_pointing_elsewhere_counts_as_off_and_can_be_rewritten(temp_value):
    """挪过文件夹、或从开发模式换成安装版之后就是这种状态：值在，但拉不起现在这个程序。"""
    write_raw(temp_value, '"C:\\别的地方\\NotionSync.exe" --tray')

    assert autostart.registered() != ""
    assert autostart.is_enabled() is False

    autostart.enable()

    assert autostart.is_enabled() is True
