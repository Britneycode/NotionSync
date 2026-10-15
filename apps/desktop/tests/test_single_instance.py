"""单实例：第二个进程把文件夹交给第一个。

不真起第二个进程——`forward` 只是「连一下命名管道写一行」，同一个进程里起一个
server 就能把这条路走完。
"""

import getpass
import uuid

import pytest
from PySide6.QtNetwork import QLocalSocket

from notion_sync_desktop import __main__ as entry
from notion_sync_desktop.services.single_instance import SingleInstance, server_name


@pytest.fixture
def name():
    """每次一个独一无二的名字：绝不能撞上真正在跑的那个界面。"""
    return f"notion-sync-test-{uuid.uuid4().hex}"


@pytest.fixture
def listener(name):
    instance = SingleInstance(name)
    assert instance.listen() is True
    yield instance
    instance.close()


def collect(instance) -> list[dict]:
    received: list[dict] = []
    instance.messageReceived.connect(received.append)
    return received


# ---------------------------------------------------------------- 命名


def test_server_name_is_per_user():
    assert server_name() == f"notion-sync-desktop-{getpass.getuser()}"


# ---------------------------------------------------------------- 转交


def test_forward_without_a_listener_returns_false(name):
    assert SingleInstance(name).forward("D:/Demo/003") is False


def test_forward_reaches_the_listening_instance(name, listener, qtbot):
    received = collect(listener)
    client = SingleInstance(name)  # 留着引用：转发方也是 QObject，别让它写完就被回收

    assert client.forward("D:/Demo/003")
    qtbot.waitUntil(lambda: bool(received), timeout=3000)

    assert received == [{"folder": "D:/Demo/003"}]


def test_forward_without_a_folder_still_wakes_the_window(name, listener, qtbot):
    """`notion-sync-desktop` 第二次启动：没有参数，也得让窗口冒出来。"""
    received = collect(listener)
    client = SingleInstance(name)

    assert client.forward("")
    qtbot.waitUntil(lambda: bool(received), timeout=3000)

    assert received == [{"folder": ""}]


def test_a_message_split_across_writes_is_reassembled(name, listener, qtbot):
    """管道可能分几次送到，凑够换行才算一条。"""
    received = collect(listener)

    socket = QLocalSocket()
    socket.connectToServer(name)
    assert socket.waitForConnected(2000)
    socket.write(b'{"folder": "D:/De')
    socket.flush()
    socket.waitForBytesWritten(2000)
    socket.write(b'mo/003"}\n')
    socket.flush()
    socket.waitForBytesWritten(2000)

    qtbot.waitUntil(lambda: bool(received), timeout=3000)
    assert received == [{"folder": "D:/Demo/003"}]
    socket.close()


# ---------------------------------------------------------------- 命令行


def test_folder_argument_picks_the_first_existing_directory(tmp_path):
    folder = tmp_path / "sync-root"
    folder.mkdir()

    assert entry.folder_argument(["app.exe", str(tmp_path / "没有"), str(folder)]) == str(
        folder.resolve()
    )


def test_folder_argument_is_empty_without_a_directory():
    assert entry.folder_argument(["app.exe", "--theme", "dark"]) == ""
    assert entry.folder_argument(["app.exe"]) == ""
