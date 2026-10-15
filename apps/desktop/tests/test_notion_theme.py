"""Notion 主题覆盖（`services/notion_theme.py`）。

这里只测真会生效的三处：窗口底色、侧栏/内容区的 `setCustomStyleSheet`、原生控件的
QSS。qfluentwidgets 的组件是自绘的（QPainter），窗口级 QSS 打不进去——别照着
"应该能改"的直觉往上加规则，加了也不生效，只会让人以为改过了。
"""

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentWindow, isDarkTheme, setTheme

from notion_sync_desktop.services import notion_theme as mod


@pytest.fixture
def restore_theme():
    """`apply_theme` 改的是全局主题，用完还原，免得串到别的用例。"""
    from qfluentwidgets.common.config import qconfig

    before = qconfig.theme
    yield
    setTheme(before)


@pytest.fixture
def window(qtbot):
    win = FluentWindow()
    qtbot.addWidget(win)
    return win


# ---------------------------------------------------------------- 窗口底色


def test_install_paints_the_window_notion_white(window, restore_theme):
    mod.install(window, "light")

    assert not window.isMicaEffectEnabled()  # 开着毛玻璃底色会被冲掉
    assert window._normalBackgroundColor() == QColor(mod.LIGHT["bg"])


def test_dark_mode_gets_its_own_background(window, restore_theme):
    mod.install(window, "dark")

    assert isDarkTheme()
    assert window._normalBackgroundColor() == QColor(mod.DARK["bg"])


# ---------------------------------------------------------------- 侧栏


def test_sidebar_is_expanded_and_wears_the_notion_surface(window, restore_theme):
    """Fluent 默认是 48px 的透明图标条；Notion 是带文字的暖灰常驻栏。"""
    mod.install(window, "light")

    panel = window.navigationInterface.panel
    assert panel.width() > 48
    assert mod.LIGHT["surface"] in panel.styleSheet()


def test_dark_sidebar_uses_the_dark_surface(window, restore_theme):
    mod.install(window, "dark")

    assert mod.DARK["surface"] in window.navigationInterface.panel.styleSheet()


# ---------------------------------------------------------------- 内容区


def test_install_flattens_the_content_area(window, restore_theme):
    """Fluent 给内容区盖了一层 3% 白 + 圆角；Notion 的内容区和窗口齐平。"""
    mod.install(window, "light")

    assert "background-color: transparent" in window.stackedWidget.styleSheet()


# ---------------------------------------------------------------- 全局 QSS


def test_switching_the_theme_swaps_the_native_control_qss(restore_theme):
    mod.apply_theme("dark")
    assert mod.DARK["text"] in QApplication.instance().styleSheet()

    mod.apply_theme("light")
    assert mod.LIGHT["text"] in QApplication.instance().styleSheet()


def test_apply_theme_survives_without_a_running_app(restore_theme, monkeypatch):
    """启动早期就调过它，那时可能还没有 QApplication。"""
    monkeypatch.setattr(mod.QApplication, "instance", staticmethod(lambda: None))
    mod.apply_theme("dark")  # 不抛就算过
