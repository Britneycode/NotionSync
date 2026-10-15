"""调 VS Code 做「本地 ↔ 远端副本」对比（设计 §226）。

只管一件事：找到 `code` 命令，按 `code --diff A B` 起一个独立进程。
找不到就抛 `CodeNotFound`，由调用方提示用户——界面不该自己装编辑器。
"""

import os
import shutil
import subprocess
from pathlib import Path

# PATH 里没有时的兜底：安装器把 bin 目录写进的是「用户 PATH」，
# 而在安装之前就已经启动的进程看不到那次更新。
_FALLBACKS = (
    ("LOCALAPPDATA", "Programs/Microsoft VS Code/bin/code.cmd"),
    ("ProgramFiles", "Microsoft VS Code/bin/code.cmd"),
    ("ProgramFiles(x86)", "Microsoft VS Code/bin/code.cmd"),
)


class CodeNotFound(Exception):
    """没有找到 VS Code 的命令行入口。"""


def find_code() -> str:
    """返回 `code` 的路径：先查 PATH，再查默认安装位置。"""
    found = shutil.which("code") or shutil.which("code.cmd")
    if found:
        return found
    for env, rel in _FALLBACKS:
        base = os.environ.get(env)
        if base and (path := Path(base) / rel).is_file():
            return str(path)
    raise CodeNotFound("没找到 VS Code 的 code 命令")


def build_argv(code: str, left: str | Path, right: str | Path) -> list[str]:
    """拼出启动参数。Windows 上 `code` 是 .cmd，CreateProcess 不能直接跑批处理。"""
    args = ["--diff", str(left), str(right)]
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", code, *args]
    return [code, *args]


def open_diff(left: str | Path, right: str | Path) -> None:
    """在 VS Code 里并排打开两个文件的差异。"""
    code = find_code()
    kwargs = {}
    if os.name == "nt":
        # 借道 cmd 会闪一个黑框，用 CREATE_NO_WINDOW 压掉
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(build_argv(code, left, right), **kwargs)
