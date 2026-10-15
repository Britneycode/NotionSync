"""开机自启：HKCU 的 Run 键下的一条值（Windows 专用）。

安装包也写同一条值（`packaging/installer.iss` 的 `[Registry]`），两边必须对得上——
值名 `NotionSync`，命令 `"<exe>" --tray`（`--tray` 不能少，见 DECISIONS）。设置页里
的开关就是这条值的增删，跟卸载时删掉它是同一个动作。

开发模式下写的是当前解释器（`python.exe -m notion_sync_desktop --tray`），能跑通但
绑在 .venv 上；装好之后用安装版再开一次，值会被改写成安装目录里的 exe。
"""

import sys
import winreg

VALUE_NAME = "NotionSync"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launcher() -> str:
    """开机要跑的那条命令行。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    return f'"{sys.executable}" -m notion_sync_desktop --tray'


def _exe_of(command: str) -> str:
    """从注册表里那条命令里取出 exe 路径：有引号取引号里的，没有就取第一个空格前。"""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 0:
            return command[1:end]
    return command.split(" ", 1)[0]


def registered() -> str:
    """注册表里当前那条命令；没有这条值就是空串。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return ""
    return str(value)


def is_enabled() -> bool:
    """开机时会不会拉起**这个**程序。

    值存在但指向别的 exe 时算「关」——挪过文件夹、或者从开发模式换成安装版之后就是
    这种状态，那条值其实拉不起任何东西。这时打开开关会用当前路径重写它。
    """
    exe = _exe_of(registered())
    return bool(exe) and exe.casefold() == str(sys.executable).casefold()


def enable() -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launcher())


def disable() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:  # 键或值本来就没有，当成已经关掉
        pass
