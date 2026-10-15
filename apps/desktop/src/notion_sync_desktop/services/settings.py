"""界面偏好（设计 v2 §6.1）。

只存界面自己的东西——记不住上次用的文件夹会很难用，但**不存 token**
（token 走 `sync_lib.credentials` 进 Windows 凭据管理器，见 DECISIONS「机密不落盘」）。
"""

import json
from pathlib import Path

from platformdirs import user_config_dir

APP_NAME = "NotionSync"


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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
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
