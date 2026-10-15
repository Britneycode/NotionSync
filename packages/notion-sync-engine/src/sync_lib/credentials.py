"""Token 解析与保存（设计 v2 §7.6）。

读取顺序：环境变量 `NOTION_TOKEN` → Windows 凭据管理器（keyring）。
token 不经过界面进程，也不出现在命令行参数里。
"""

import os

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "notion-sync"
ACCOUNT = "default"


def get_token() -> tuple[str | None, str]:
    """返回 (token, 来源)；来源为 "env" / "keyring" / "none"。"""
    if t := os.environ.get("NOTION_TOKEN"):
        return t, "env"
    if t := keyring.get_password(SERVICE, ACCOUNT):
        return t, "keyring"
    return None, "none"


def save_token(token: str) -> None:
    """先验证 token 有效再落盘；无效时 notion_client 会抛 APIResponseError。"""
    from notion_client import Client

    Client(auth=token).users.me()
    keyring.set_password(SERVICE, ACCOUNT, token)


def delete_token() -> None:
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except PasswordDeleteError:
        pass
