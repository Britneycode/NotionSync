"""界面偏好（设计 v2 §6.1）。

只存界面自己的东西——记不住上次用的文件夹会很难用，但**不存 token**
（token 走 `sync_lib.credentials` 进 Windows 凭据管理器，见 DECISIONS「机密不落盘」）。
"""

import json
from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "NotionSync"

THEMES = ("auto", "light", "dark")
DEFAULT_THEME = "auto"


def config_path() -> Path:
    return Path(user_config_dir(APP_NAME)) / "settings.json"


class UiSettings:
    """一个很小的键值表，读写都容忍文件缺失/损坏。"""

    def __init__(self, path: Path | None = None):
        self._path = path or config_path()
        self._data: dict = self._read()

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        """写之前先把磁盘上的读回来合并。

        同步页、设置页各持一个实例，都只记得自己构造时的快照；直接整份覆盖
        会让后写的那个把前一个刚存的键抹掉（比如改主题把「上次的文件夹」冲掉）。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        merged = {**self._read(), **self._data}
        self._path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def last_folder(self) -> str:
        folder = self._data.get("last_folder", "")
        # 文件夹可能已经被删/移走，别把一个不存在的路径塞回输入框
        return folder if folder and Path(folder).is_dir() else ""

    @last_folder.setter
    def last_folder(self, value: str) -> None:
        self._data["last_folder"] = value
        self.save()

    @property
    def bookmarks(self) -> list[str]:
        """常用文件夹。已经不在的路径不列出来（跟 `last_folder` 一个口径）。"""
        items = self._data.get("bookmarks")
        if not isinstance(items, list):
            return []
        return [p for p in items if isinstance(p, str) and Path(p).is_dir()]

    def add_bookmark(self, folder: str) -> bool:
        """加一个书签；已经在里面就返回 False。新加的排最前。"""
        folder = folder.strip()
        if not folder:
            return False
        items = self._data.get("bookmarks")
        items = items if isinstance(items, list) else []
        if folder in items:
            return False
        self._data["bookmarks"] = [folder, *items]
        self.save()
        return True

    def remove_bookmark(self, folder: str) -> None:
        items = self._data.get("bookmarks")
        if isinstance(items, list) and folder in items:
            self._data["bookmarks"] = [p for p in items if p != folder]
            self.save()

    @property
    def watch_folder(self) -> bool:
        """文件夹一有改动就自动跑一次增量同步（默认开）。"""
        return bool(self._data.get("watch_folder", True))

    @watch_folder.setter
    def watch_folder(self, value: bool) -> None:
        self._data["watch_folder"] = bool(value)
        self.save()

    @property
    def theme(self) -> str:
        value = self._data.get("theme")
        return value if value in THEMES else DEFAULT_THEME

    @theme.setter
    def theme(self, value: str) -> None:
        self._data["theme"] = value if value in THEMES else DEFAULT_THEME
        self.save()
