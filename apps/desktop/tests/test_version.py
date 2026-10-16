"""发版约束：版本号散在两个 pyproject、两个 `__init__` 和 Inno 脚本里。

改了一处忘了别处是发版最常见的低级错误（装出来的包版本号对不上、升级判断出错），
所以用测试把四处钉死——只改一半就会红。
"""

import re
import tomllib
from pathlib import Path

import sync_lib
from notion_sync_desktop import __version__ as desktop_version

ROOT = Path(__file__).resolve().parents[3]  # apps/desktop/tests/ → 仓库根


def _pyproject_version(relative: str) -> str:
    data = tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))
    return data["project"]["version"]


def _installer_text() -> str:
    return (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")


def test_desktop_version_agrees_with_installer():
    assert _pyproject_version("apps/desktop/pyproject.toml") == desktop_version
    # installer.iss 只有 AppVersion 一处声明版本，安装包的卸载项与「升级判断」都读它
    assert re.search(r'#define AppVersion "([^"]+)"', _installer_text()).group(1) == (
        desktop_version
    )


def test_engine_version_agrees_with_pyproject():
    assert _pyproject_version("packages/notion-sync-engine/pyproject.toml") == (
        sync_lib.__version__
    )


def test_installer_output_name_carries_the_version():
    """安装包文件名必须带版本号：升级时用户与我们要能一眼分清新旧包。"""
    assert "OutputBaseFilename=NotionSync-Setup-{#AppVersion}" in _installer_text()
