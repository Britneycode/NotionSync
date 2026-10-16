"""检查更新：匿名查 GitHub Releases，只提示、不自动下载（设计 §"版本更新"）。

仓库是公开的，`releases/latest` 匿名可查（60 次/小时/IP，一次启动查一次绰绰有余），
不需要任何 token。查不到（没网、限流、还没发过 release、返回不是 JSON）一律当作
「不知道」——这是可选功能，不该变成噪音源。
"""

import json
import re
import urllib.request

from PySide6.QtCore import QThread, Signal

REPO = "Britneycode/NotionSync"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

# GitHub 要求带 User-Agent，否则 403
_HEADERS = {"User-Agent": "NotionSync-update-check"}


def parse_version(text) -> tuple[int, ...] | None:
    """把 `v2.1.0` / `2.1.0` 解析成 `(2, 1, 0)`；解析不了返回 None。"""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(remote, local) -> bool:
    """远端版本是否比本地新。按数值比，不是按字符串（`2.10.0` > `2.9.0`）。"""
    remote_parts, local_parts = parse_version(remote), parse_version(local)
    if remote_parts is None or local_parts is None:
        return False
    return remote_parts > local_parts


def fetch_latest(timeout: float = 10.0) -> dict | None:
    """查最新 release，返回 `{"tag", "url"}`；任何异常都返回 None。"""
    try:
        request = urllib.request.Request(API_LATEST, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        tag = data.get("tag_name")
        if not tag:
            return None
        return {"tag": tag, "url": data.get("html_url") or RELEASES_PAGE}
    except Exception:
        # 没网、超时、404（还没发过 release）、403（限流）、响应不是 JSON……
        # 通通当作「查不到」，界面静默
        return None


class UpdateChecker(QThread):
    """后台查一次最新 release，别卡住界面（沿用设置页 `_TokenSaver` 的写法）。

    `result` 的三种状态：`newer`（有新版）/ `current`（已是最新）/ `error`（查不到）。
    启动时的自动检查只在 `newer` 时吭声；用户手动点「检查更新」三种都给反馈。
    """

    result = Signal(str, str, str)  # state, version, url

    def __init__(self, local_version: str, fetcher=fetch_latest, parent=None):
        super().__init__(parent)
        self._local = local_version
        self._fetch = fetcher

    def run(self) -> None:
        data = self._fetch()
        if not data:
            self.result.emit("error", "", "")
        elif is_newer(data.get("tag", ""), self._local):
            self.result.emit("newer", data["tag"], data.get("url") or RELEASES_PAGE)
        else:
            self.result.emit("current", data.get("tag", ""), "")
