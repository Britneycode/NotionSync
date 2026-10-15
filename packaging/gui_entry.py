"""PyInstaller 入口：桌面界面（打包用）。

不能直接把 `notion_sync_desktop/__main__.py` 当入口——PyInstaller 会把入口脚本
按顶层 `__main__` 执行，那份文件里的相对导入（`from .main_window import ...`）
就没有包上下文了。这里用绝对导入绕开，行为与 `python -m notion_sync_desktop` 一致。
"""

import sys

from notion_sync_desktop.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
