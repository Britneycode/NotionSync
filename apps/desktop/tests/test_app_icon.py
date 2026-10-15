"""应用图标：窗口 / 任务栏与托盘共用一份，路径写错只会静默退回 Windows 通用图标。"""

from notion_sync_desktop.services import app_icon as app_icon_module
from notion_sync_desktop.services.app_icon import app_icon, icon_path


def test_icon_path_points_at_a_loadable_icon(qapp):
    """路径不对时 `QIcon` 只给一个空图标，界面上就是 Windows 的通用图标——得读得出来。"""
    path = icon_path()

    assert path.is_file()
    assert not app_icon().isNull()


def test_icon_path_looks_under_meipass_when_frozen(monkeypatch, tmp_path):
    """打包后图标在 `_internal/` 下（spec 的 `datas`），不在源码树里。"""
    monkeypatch.setattr(app_icon_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_icon_module.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert icon_path() == tmp_path / "notion_sync_desktop" / "resources" / "icon.ico"
