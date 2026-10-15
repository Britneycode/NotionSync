"""`EngineClient` 的进程级测试：真起一个假引擎子进程，看事件流对不对。

不 mock QProcess——这一层的价值全在「真进程 + 真管道」上，假掉就白测了。
"""

import sys
from pathlib import Path

import pytest

from notion_sync_desktop import engine_client
from notion_sync_desktop.engine_client import EngineClient

FAKE_ENGINE = str(Path(__file__).with_name("fake_engine.py"))

# 子进程启动 + 事件往返在慢机器上可能到秒级，留足余量
RUN_TIMEOUT = 30_000


class Recorder:
    """把 EngineClient 发出来的四路信号都记下来，方便断言。"""

    def __init__(self, client: EngineClient):
        self.events: list[dict] = []
        self.diagnostics: list[str] = []
        self.results: list[tuple[int, bool]] = []
        self.running: list[bool] = []
        client.event.connect(self.events.append)
        client.diagnostic.connect(self.diagnostics.append)
        client.finished.connect(lambda code, crashed: self.results.append((code, crashed)))
        client.runningChanged.connect(self.running.append)

    @property
    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    @property
    def diag(self) -> str:
        return "".join(self.diagnostics)


@pytest.fixture
def clients():
    """持有所有造出来的 client，别让它们被 GC 掉（QProcess 是它的孩子）。"""
    made: list[EngineClient] = []
    yield made
    for c in made:
        if c.is_running():
            c._proc.kill()
            c._proc.waitForFinished(5000)


@pytest.fixture
def start_run(monkeypatch, clients):
    """start_run(mode, grace_ms=None) -> (client, recorder)，不自动等待结束。

    grace_ms 必须在这里设：强杀定时器的间隔是构造时从类属性取的，
    构造完再改实例属性不生效。
    """

    def _start(mode: str, grace_ms: int | None = None, command: str = "sync"):
        monkeypatch.setattr(
            engine_client,
            "engine_command",
            lambda: (sys.executable, [FAKE_ENGINE, "--fake-mode", mode]),
        )
        if grace_ms is not None:
            monkeypatch.setattr(EngineClient, "CANCEL_GRACE_MS", grace_ms)
        client = EngineClient()
        clients.append(client)
        rec = Recorder(client)
        client.start(command, "D:/tmp/root")
        return client, rec

    return _start


def wait_finished(qtbot, rec: Recorder, client: EngineClient):
    """等这一轮结束，返回 (exit_code, crashed)。

    不能等信号：启动失败时 QProcess 在 `start()` 里就把 errorOccurred 发完了，
    我们的 finished 也是同步发出去的，等新信号会一直等不到。所以等结果列表。
    """
    qtbot.waitUntil(lambda: bool(rec.results), timeout=RUN_TIMEOUT)
    assert not client.is_running()
    return rec.results[-1]


# --------------------------------------------------------------- 命令行拼装


def test_engine_command_uses_module_in_dev_mode(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    program, base = engine_client.engine_command()
    assert program == sys.executable
    assert base == ["-m", "sync_lib.cli"]


def test_engine_command_prefers_windowed_sibling_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "NotionSync.exe"))
    program, base = engine_client.engine_command()
    # 窗口版（不是同目录那个控制台的 notion-sync.exe）：界面没控制台，拉控制台
    # 程序会闪黑框。见 DECISIONS
    assert program == str(tmp_path / "notion-sync-engine.exe")
    assert base == []


# --------------------------------------------------------------- 正常一轮


def test_normal_run_streams_events_then_finishes(qtbot, start_run):
    client, rec = start_run("normal")
    assert wait_finished(qtbot, rec, client) == (0, False)
    assert rec.types == [
        "hello",
        "run.started",
        "phase",
        "plan",
        "file.started",
        "file.finished",
        "progress",
        "run.finished",
    ]
    assert rec.events[5]["data"]["path"] == "a.md"
    assert rec.running == [True, False]
    assert not client.is_running()


def test_root_and_flags_are_passed_through(qtbot, start_run):
    client, rec = start_run("normal")
    with qtbot.waitSignal(client.finished, timeout=RUN_TIMEOUT):
        pass
    root = next(e for e in rec.events if e["type"] == "run.started")["data"]["root"]
    assert root == "D:/tmp/root"


def test_start_while_running_raises(qtbot, start_run):
    client, _ = start_run("ignore-cancel", grace_ms=400)
    qtbot.waitUntil(client.is_running, timeout=RUN_TIMEOUT)
    with pytest.raises(RuntimeError):
        client.start("sync", "D:/tmp/root")


# --------------------------------------------------------------- 协议解析


def test_half_line_is_reassembled(qtbot, start_run):
    """一次 readyRead 只到半个 JSON 时不能当成坏数据。"""
    _, rec = start_run("split")
    qtbot.waitUntil(lambda: bool(rec.events), timeout=RUN_TIMEOUT)
    assert rec.types == ["hello"]
    assert rec.events[0]["data"] == {"a": 1}
    assert rec.diag == ""


def test_non_protocol_line_goes_to_diagnostic(qtbot, start_run):
    _, rec = start_run("garbage")
    qtbot.waitUntil(lambda: bool(rec.results), timeout=RUN_TIMEOUT)
    assert rec.types == ["hello"]  # 坏行不影响后面的好行
    assert "[非协议输出] 这行不是 JSON" in rec.diag


def test_protocol_version_mismatch_is_dropped(qtbot, start_run):
    _, rec = start_run("badversion")
    qtbot.waitUntil(lambda: bool(rec.results), timeout=RUN_TIMEOUT)
    assert rec.events == []
    assert "协议版本不匹配：99" in rec.diag


def test_trailing_partial_line_is_surfaced(qtbot, start_run):
    """引擎写一半就死：最后那段要露出来，不能悄悄丢。"""
    _, rec = start_run("tail")
    qtbot.waitUntil(lambda: bool(rec.results), timeout=RUN_TIMEOUT)
    assert rec.types == ["hello"]
    assert '[非协议输出] {"v":1,"seq":2,"type":"run.finis' in rec.diag


def test_stderr_is_forwarded_as_diagnostic(qtbot, start_run):
    """顺带验证子进程被强制成 UTF-8，中文不会变成乱码。"""
    _, rec = start_run("stderr")
    qtbot.waitUntil(lambda: bool(rec.results), timeout=RUN_TIMEOUT)
    assert "引擎自己的一行日志" in rec.diag


# --------------------------------------------------------------- 退出与取消


def test_crash_exit_code_is_reported(qtbot, start_run):
    client, rec = start_run("crash")
    code, _crashed = wait_finished(qtbot, rec, client)
    assert code == 3


def test_cancel_reaches_engine_and_returns_130(qtbot, start_run):
    client, rec = start_run("cancel")
    qtbot.waitUntil(lambda: "file.started" in rec.types, timeout=RUN_TIMEOUT)
    client.cancel()
    assert wait_finished(qtbot, rec, client) == (130, False)
    assert "run.cancelled" in rec.types


def test_cancel_force_kills_engine_that_ignores_it(qtbot, start_run):
    client, rec = start_run("ignore-cancel", grace_ms=400)
    qtbot.waitUntil(client.is_running, timeout=RUN_TIMEOUT)
    client.cancel()
    _code, crashed = wait_finished(qtbot, rec, client)
    assert crashed is True
    assert "取消超时，强制结束引擎" in rec.diag


def test_failed_to_start_reports_minus_one(qtbot, monkeypatch, clients):
    monkeypatch.setattr(
        engine_client, "engine_command", lambda: ("Z:/nope/notion-sync.exe", [])
    )
    client = EngineClient()
    clients.append(client)
    rec = Recorder(client)
    client.start("sync", "D:/tmp/root")
    assert wait_finished(qtbot, rec, client) == (-1, True)
    assert "引擎启动失败" in rec.diag
    assert rec.running == [True, False]
    assert not client.is_running()


def test_cancel_without_running_is_a_noop(qtbot, start_run):
    client, rec = start_run("normal")
    wait_finished(qtbot, rec, client)
    client.cancel()
    assert not client._kill_timer.isActive()
    assert rec.results == [(0, False)]


def test_pages_mode_emits_the_page_list(qtbot, start_run):
    """只读查询：hello → run.started → pages → run.finished，中间没有任何执行事件。"""
    client, rec = start_run("pages", command="pages")

    assert wait_finished(qtbot, rec, client) == (0, False)

    assert rec.types == ["hello", "run.started", "pages", "run.finished"]
    items = rec.events[2]["data"]["items"]
    assert [p["title"] for p in items] == ["工作台", "归档"]
