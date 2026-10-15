"""单实例（设计 v2 §4.4）。

第二次启动不开第二个窗口，而是把文件夹参数转交给已经在跑的那个，然后自己退出。
用 `QLocalServer` / `QLocalSocket`：Windows 上底层是命名管道，不引入额外依赖。
"""

import getpass
import json

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

CONNECT_TIMEOUT_MS = 500


def server_name() -> str:
    """带上用户名：同一台机器上不同账户各跑各的，不互相抢。"""
    return f"notion-sync-desktop-{getpass.getuser()}"


class SingleInstance(QObject):
    """已经在跑就 `forward()` 返回 True；否则 `listen()` 当那个实例。"""

    messageReceived = Signal(dict)

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self._name = name
        self._server: QLocalServer | None = None
        self._buffers: dict[QLocalSocket, bytearray] = {}

    def forward(self, folder: str) -> bool:
        """把参数交给已经在跑的窗口；没人听就返回 False。"""
        socket = QLocalSocket(self)
        socket.connectToServer(self._name)
        if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
            return False
        socket.write((json.dumps({"folder": folder}) + "\n").encode("utf-8"))
        socket.flush()
        socket.waitForBytesWritten(CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()
        return True

    def listen(self) -> bool:
        """开始接收后来者的转交。上一个进程被强杀可能留下管道名，先清掉。"""
        QLocalServer.removeServer(self._name)
        server = QLocalServer(self)
        if not server.listen(self._name):
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None

    # ---------------------------------------------------------------- 内部

    def _on_new_connection(self) -> None:
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        self._buffers[conn] = bytearray()
        conn.readyRead.connect(lambda c=conn: self._on_ready_read(c))
        conn.disconnected.connect(lambda c=conn: self._drop(c))

    def _on_ready_read(self, conn: QLocalSocket) -> None:
        # 对面发的是一行 JSON，写完就断。管道可能分几次到，凑够换行才算一条
        buf = self._buffers.setdefault(conn, bytearray())
        buf += bytes(conn.readAll())
        if b"\n" not in buf:
            return
        line, _, _ = bytes(buf).partition(b"\n")
        self._drop(conn)
        try:
            message = json.loads(line.decode("utf-8"))
        except ValueError:
            return
        if isinstance(message, dict):
            self.messageReceived.emit(message)

    def _drop(self, conn: QLocalSocket) -> None:
        self._buffers.pop(conn, None)
        conn.close()
        conn.deleteLater()
