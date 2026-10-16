"""检查更新：匿名查 GitHub Releases，只在真有新版时吭声。

仓库是公开的，`api.github.com/.../releases/latest` 匿名可查（60 次/小时/IP，
一次启动查一次绰绰有余），所以不需要任何 token。
"""

import json

from notion_sync_desktop.services import update_check as mod
from notion_sync_desktop.services.update_check import (
    UpdateChecker,
    fetch_latest,
    is_newer,
    parse_version,
)


class _Response:
    """`urlopen` 的替身：只需要 read()。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_parse_version_accepts_v_prefix_and_bare():
    assert parse_version("v2.1.0") == (2, 1, 0)
    assert parse_version("2.1.0") == (2, 1, 0)
    assert parse_version(" v1.0 ") == (1, 0)
    assert parse_version("nightly") is None
    assert parse_version("") is None
    assert parse_version(None) is None


def test_is_newer_compares_numerically_not_as_text():
    """2.10.0 比 2.9.0 新——按字符串比会得出相反的结论。"""
    assert is_newer("v2.10.0", "2.9.0")
    assert not is_newer("v2.9.0", "2.10.0")
    assert is_newer("v2.1.1", "2.1.0")
    assert not is_newer("v2.1.0", "2.1.0")
    # tag 解析不出来（日期式 tag、草稿名）→ 当作没有新版，别误报
    assert not is_newer("2026-10-16", "2.1.0")


def test_fetch_latest_reads_tag_and_page(monkeypatch):
    payload = {
        "tag_name": "v2.1.0",
        "html_url": "https://github.com/Britneycode/NotionSync/releases/tag/v2.1.0",
    }
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda *a, **k: _Response(json.dumps(payload).encode())
    )

    assert fetch_latest() == {
        "tag": "v2.1.0",
        "url": "https://github.com/Britneycode/NotionSync/releases/tag/v2.1.0",
    }


def test_fetch_latest_is_silent_on_every_failure(monkeypatch):
    """没网 / 404（还没发过 release）/ 限流 / 返回不是 JSON → 一律 None，绝不上抛。"""

    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    assert fetch_latest() is None

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: _Response(b"<html>"))
    assert fetch_latest() is None

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: _Response(b"{}"))
    assert fetch_latest() is None  # 缺 tag_name

    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda *a, **k: _Response(b'{"tag_name": null}')
    )
    assert fetch_latest() is None


def test_checker_reports_newer_with_download_page(qtbot):
    checker = UpdateChecker(
        "2.0.0", fetcher=lambda: {"tag": "v2.1.0", "url": "https://example.test/dl"}
    )
    with qtbot.waitSignal(checker.result, timeout=3000) as blocker:
        checker.start()
    checker.wait(3000)

    assert blocker.args == ["newer", "v2.1.0", "https://example.test/dl"]


def test_checker_stays_quiet_when_up_to_date(qtbot):
    checker = UpdateChecker("2.1.0", fetcher=lambda: {"tag": "v2.1.0", "url": "x"})
    with qtbot.waitSignal(checker.result, timeout=3000) as blocker:
        checker.start()
    checker.wait(3000)

    assert blocker.args[0] == "current"


def test_checker_reports_error_when_lookup_fails(qtbot):
    checker = UpdateChecker("2.0.0", fetcher=lambda: None)
    with qtbot.waitSignal(checker.result, timeout=3000) as blocker:
        checker.start()
    checker.wait(3000)

    assert blocker.args[0] == "error"
