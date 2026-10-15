"""同步目标：这个文件夹同步到 Notion 的哪个页面。

`parent_page_id` 是 `init` 时选的那个页面，写在文件夹里的 `.sync-config.yaml`，数据库
就建在它下面。这里只读本地文件、不碰网络——页面的**标题**得靠引擎的 `pages` 查询去查。
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_NAME = ".sync-config.yaml"


@dataclass(frozen=True)
class Target:
    parent_page_id: str

    @property
    def url(self) -> str:
        """Notion 页面链接：id 去掉横线就是 URL 里那一段。"""
        return f"https://www.notion.so/{self.parent_page_id.replace('-', '')}"


def read_target(folder) -> Target | None:
    """读这个文件夹的目标页面；没初始化 / 配置坏掉 / 缺字段都返回 None，不抛。"""
    try:
        text = (Path(folder) / CONFIG_NAME).read_text(encoding="utf-8")
        config = yaml.safe_load(text)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(config, dict):
        return None
    notion = config.get("notion")
    if not isinstance(notion, dict):
        return None
    parent = notion.get("parent_page_id")
    return Target(str(parent)) if parent else None
