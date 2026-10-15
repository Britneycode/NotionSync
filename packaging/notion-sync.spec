# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单 spec：一次打出三个程序，共用同一个 onedir（设计 v2 §6.4）。

产物（dist/NotionSync/）：
- NotionSync.exe          界面（windowed）
- notion-sync.exe         引擎 CLI（console，命令行里可以直接用）
- notion-sync-engine.exe  引擎（windowed，界面拉起时用）

为什么要有第三个 exe：界面自己是 windowed 程序（没有控制台），它拉起的 console
子进程会被 Windows 新分配一个控制台——每次同步闪一个黑框。PySide6 没绑定
`QProcess.setCreateProcessArgumentsModifier`，拿不到 `CREATE_NO_WINDOW`
（见 DECISIONS），所以另出一个 windowed 版引擎专供界面调用；命令行用户的
`notion-sync.exe` 行为不变。
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent
DESKTOP_SRC = ROOT / "apps" / "desktop" / "src"
ENGINE_SRC = ROOT / "packages" / "notion-sync-engine" / "src"
PACKAGING = ROOT / "packaging"
RESOURCES = DESKTOP_SRC / "notion_sync_desktop" / "resources"
ICON = str(RESOURCES / "icon.ico")

# 两个 src 根都显式给出：editable 安装靠 .pth 也能找到，但不依赖它更稳
pathex = [str(DESKTOP_SRC), str(ENGINE_SRC)]

# 图标同时要作为**数据文件**带上：exe 里嵌的那份给资源管理器看，窗口与任务栏
# 走的是 `app.setWindowIcon()` 读的这份（见 __main__.icon_path）。只有界面需要。
gui_datas = [(str(RESOURCES / "icon.ico"), "notion_sync_desktop/resources")]

gui = Analysis([str(PACKAGING / "gui_entry.py")], pathex=pathex, datas=gui_datas)
engine = Analysis([str(PACKAGING / "engine_entry.py")], pathex=pathex)

gui_pyz = PYZ(gui.pure)
engine_pyz = PYZ(engine.pure)

# upx 关掉：压缩会让杀毒软件更容易误报（见 RISKS），体积也不是瓶颈
common = dict(exclude_binaries=True, strip=False, upx=False, icon=ICON)

gui_exe = EXE(gui_pyz, gui.scripts, [], name="NotionSync", console=False, **common)
cli_exe = EXE(engine_pyz, engine.scripts, [], name="notion-sync", console=True, **common)
# 界面拉起的那个：启动就崩时别弹 PyInstaller 的窗口化异常对话框（没人会去点，
# 只会卡住），stderr 由界面收进日志
engine_exe = EXE(
    engine_pyz,
    engine.scripts,
    [],
    name="notion-sync-engine",
    console=False,
    disable_windowed_traceback=True,
    **common,
)

COLLECT(
    gui_exe,
    cli_exe,
    engine_exe,
    gui.binaries,
    gui.datas,
    engine.binaries,
    engine.datas,
    strip=False,
    upx=False,
    name="NotionSync",
)
