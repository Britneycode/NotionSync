"""PyInstaller 入口：引擎 CLI（打包用，console 与 windowed 两个 exe 共用同一份）。"""

import sys

from sync_lib.cli import main

if __name__ == "__main__":
    sys.exit(main())
