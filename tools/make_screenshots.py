"""生成 README 用的界面截图（离屏渲染，不开真窗口）。

    uv run --project apps/desktop python tools/make_screenshots.py

产物：`assets/screenshots/{sync,plan,history,settings}.png`

三条约束，都是为了「图是真的，但不泄露本机」：

1. `WA_DontShowOnScreen` + 真实平台插件：窗口照常布局、字体和 DPI 都是本机那份，
   但不上屏，不会弹窗打扰人。（试过 `QT_QPA_PLATFORM=offscreen`——那个平台没有
   字体回退，整屏中文全是豆腐块。）
2. 把 `platformdirs` 的两个路径函数换成临时目录：设置和历史都读写在临时目录里，
   碰不到用户真实的那份。**别改 `LOCALAPPDATA` 环境变量**——Windows 上
   `platformdirs` 走 `SHGetFolderPath`，根本不看它（踩过这个坑：脚本把用户真实的
   `settings.json` 和运行历史写花了）。`_assert_isolated()` 是这道隔离的保险丝。
3. 界面内容全部经**真实入口**灌入——协议事件走 `MainWindow._on_event`，历史先由
   `HistoryWriter` 落盘再由 `HistoryPage` 自己读回，页面文本没有一处是直接
   `setText` 摆出来的。唯一的例外是 `_stub_offline()`，理由见那里的注释。

脚本会真的建两个示例文件夹（`%PUBLIC%\我的笔记`、`%PUBLIC%\工作周报`）——截图里
的路径要指向真实存在的目录，`refresh_folder_state` 才肯显示，跑完即删。
"""

import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import platformdirs  # 拿模块本身：要在应用代码 `from platformdirs import …` 之前换掉它

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "desktop" / "src"))

# --- 环境隔离：必须早于任何 notion_sync_desktop 的导入 ---
# 放在 %PUBLIC% 下而不是 %TEMP%：历史页会把「记录目录」原样显示出来，走 %TEMP%
# 会在图里露出 C:\Users\<用户名>\…。
WORK = Path(os.environ["PUBLIC"]) / "NotionSyncShots"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)
platformdirs.user_data_dir = lambda *a, **k: str(WORK)
platformdirs.user_config_dir = lambda *a, **k: str(WORK)
os.environ["QT_SCALE_FACTOR"] = "1.5"  # 1x 的字在 README 里发虚
os.environ.pop("NOTION_TOKEN", None)

DEMO_HOME = Path(os.environ["PUBLIC"])
NOTES = DEMO_HOME / "我的笔记"
REPORTS = DEMO_HOME / "工作周报"
OUT = ROOT / "assets" / "screenshots"

PAGE_ID = "3f2a7c41-8b6d-4e05-9a17-2c5d8e6f0b34"
PAGE_TITLE = "我的笔记"

# 示例文件树：路径要像真的，才看得出「中文目录 + 层级」都能同步
NOTES_TREE = {
    "README.md": "# 我的笔记\n\n本地文件夹与 Notion 双向同步的内容都在这里。\n",
    "算法/排序.md": "# 排序\n\n快排、归并、堆排的时间复杂度与稳定性对比。\n",
    "算法/动态规划.md": "# 动态规划\n\n状态定义 → 转移方程 → 边界；背包问题的三种写法。\n",
    "算法/图论.md": "# 图论\n\n最短路、最小生成树、拓扑排序。\n",
    "读书/《深入理解计算机系统》笔记.md": "# CSAPP\n\n第 6 章 存储器层次结构。\n",
    "读书/2026-阅读清单.md": "- [x] 《代码整洁之道》\n- [ ] 《设计数据密集型应用》\n",
    "项目/notion-sync 设计.md": "# 设计\n\n文件夹为真相源，Notion 数据库是投影。\n",
    "项目/周报模板.md": "## 本周\n\n## 下周\n",
    "随手记/想法收集.md": "- 同步冲突时先备份再裁决\n",
}
REPORTS_TREE = {
    "README.md": "# 工作周报\n",
    "2026-W42.md": "## 完成\n\n## 风险\n",
}

# 一次增量同步的计划。动作取值见 plan_model.FILTER_GROUPS，local/remote 见 SIDE_LABELS
PLAN_ROWS = [
    {"action": "PUSH", "path": "算法/排序.md", "local": "changed", "remote": "same"},
    {"action": "PUSH", "path": "算法/动态规划.md", "local": "changed", "remote": "same"},
    {"action": "PULL", "path": "算法/图论.md", "local": "same", "remote": "changed"},
    {"action": "RENAME", "path": "随手记/想法收集.md", "local": "same", "remote": "changed"},
    {"action": "CREATE_REMOTE", "path": "读书/《深入理解计算机系统》笔记.md", "local": "new", "remote": "absent"},
    {"action": "CREATE_REMOTE", "path": "项目/notion-sync 设计.md", "local": "new", "remote": "absent"},
    {"action": "CREATE_REMOTE", "path": "项目/周报模板.md", "local": "new", "remote": "absent"},
    {"action": "SKIP", "path": "README.md", "local": "same", "remote": "same"},
    {"action": "SKIP", "path": "读书/2026-阅读清单.md", "local": "same", "remote": "same"},
]
COUNTS = {"PUSH": 2, "PULL": 1, "RENAME": 1, "CREATE_REMOTE": 3, "SKIP": 2}
SUMMARY = {
    "pushed": 2, "pulled": 1, "created": 3, "renamed": 1,
    "conflicts": 0, "skipped": 2, "failed": 0,
}
FINGERPRINT = "a3f1c9e2"

# 喂到「第 7 个文件的第 6 步已完成」为止：进度条有值、当前文件在跑、日志有内容
SYNC_EVENTS = [
    ("run.started", {"command": "sync", "root": str(NOTES)}),
    ("phase", {"name": "scan"}),
    ("log", {"level": "info", "message": "本地 9 个文件，状态文件读到 8 条记录"}),
    ("phase", {"name": "remote"}),
    ("log", {"level": "info", "message": "远端数据库 9 个页面"}),
    ("phase", {"name": "plan"}),
    ("plan", {"fingerprint": FINGERPRINT, "rows": PLAN_ROWS, "counts": COUNTS}),
    ("phase", {"name": "execute"}),
    ("file.started", {"path": "算法/排序.md", "index": 1, "total": 7}),
    ("file.finished", {"path": "算法/排序.md", "result": "ok"}),
    ("progress", {"done": 1, "total": 7}),
    ("file.started", {"path": "算法/动态规划.md", "index": 2, "total": 7}),
    ("file.finished", {"path": "算法/动态规划.md", "result": "ok"}),
    ("progress", {"done": 2, "total": 7}),
    ("file.started", {"path": "算法/图论.md", "index": 3, "total": 7}),
    ("file.finished", {"path": "算法/图论.md", "result": "ok"}),
    ("progress", {"done": 3, "total": 7}),
    ("file.started", {"path": "随手记/想法收集.md", "index": 4, "total": 7}),
    ("file.finished", {"path": "随手记/想法收集.md", "result": "ok"}),
    ("progress", {"done": 4, "total": 7}),
    ("file.started", {"path": "读书/《深入理解计算机系统》笔记.md", "index": 5, "total": 7}),
    ("file.finished", {"path": "读书/《深入理解计算机系统》笔记.md", "result": "ok"}),
    ("progress", {"done": 5, "total": 7}),
    ("file.started", {"path": "项目/notion-sync 设计.md", "index": 6, "total": 7}),
    ("file.finished", {"path": "项目/notion-sync 设计.md", "result": "ok"}),
    ("progress", {"done": 6, "total": 7}),
    ("file.started", {"path": "项目/周报模板.md", "index": 7, "total": 7}),
]

# 三条历史：时间相对「现在」算，图放多久都不显旧
HISTORY = [
    (
        timedelta(minutes=42),
        "sync",
        {"pushed": 2, "pulled": 1, "created": 3, "renamed": 1, "skipped": 2, "failed": 0},
        PLAN_ROWS,
    ),
    (
        timedelta(days=1, hours=3),
        "sync",
        {"pushed": 1, "skipped": 8, "failed": 0},
        [{"action": "PUSH", "path": "算法/排序.md", "local": "changed", "remote": "same"}],
    ),
    (
        timedelta(days=2, hours=5),
        "push",
        {"pushed": 2, "created": 1, "skipped": 6, "failed": 0},
        [
            {"action": "PUSH", "path": "项目/周报模板.md", "local": "changed", "remote": "same"},
            {"action": "CREATE_REMOTE", "path": "读书/2026-阅读清单.md", "local": "new", "remote": "absent"},
        ],
    ),
]


def build_demo_folders() -> None:
    """建截图里出现的两个文件夹。它们得真存在，界面才肯显示。"""
    for root, tree in ((NOTES, NOTES_TREE), (REPORTS, REPORTS_TREE)):
        for rel, text in tree.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    # 让「同步到」那一行显示页面标题而不是一串 id
    (NOTES / ".sync-config.yaml").write_text(
        f"notion:\n  parent_page_id: {PAGE_ID}\n", encoding="utf-8"
    )


def write_ui_settings() -> None:
    from notion_sync_desktop.services.settings import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "last_folder": str(NOTES),
                "bookmarks": [str(NOTES), str(REPORTS)],
                "watch_folder": True,
                "theme": "light",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def write_history() -> None:
    """用真实的 `HistoryWriter` 落盘，再改名成想要的时间戳。"""
    from notion_sync_desktop.services import history

    now = datetime.now()
    for delta, command, summary, rows in HISTORY:
        writer = history.HistoryWriter()
        stamp = (now - delta).strftime(history.STAMP_FORMAT)
        writer.write({"type": "run.started", "data": {"command": command, "root": str(NOTES)}})
        writer.write(
            {
                "type": "plan",
                "data": {"fingerprint": FINGERPRINT, "rows": rows, "counts": {}},
            }
        )
        for row in rows:
            writer.write({"type": "file.finished", "data": {"path": row["path"], "result": "ok"}})
        writer.write(
            {
                "type": "run.finished",
                "data": {"exit_code": 0, "duration_ms": 12400, "summary": summary},
            }
        )
        writer.finish(0, False)
        writer.path.rename(writer.path.with_name(f"{stamp}-{command}.jsonl"))


def _assert_isolated() -> None:
    """隔离的保险丝：没生效就立刻停——宁可不出图，也不能把用户真实的数据写花。"""
    from notion_sync_desktop.services.history import runs_dir
    from notion_sync_desktop.services.settings import config_path

    for path in (config_path(), runs_dir()):
        if WORK not in path.parents:
            raise SystemExit(f"隔离失效：{path} 不在 {WORK} 下，拒绝运行")


def _stub_offline() -> None:
    """把三处「本机状态」钉成演示用的值。

    它们都不是摆拍界面文本，只是让脚本不去碰本机：
    - token 来源：真读 keyring 会显示用户自己存没存过 token；这里钉成「已存进凭据
      管理器」——README 展示的是配好之后的样子，而且 `refresh()` 只显示**来源**，
      不显示 token 本身。
    - 自动检查更新：`QTimer.singleShot(3000, …)` 排好了取消不掉，换成空实现，
      免得脚本跑着跑着去连 GitHub。
    """
    from notion_sync_desktop import main_window
    from notion_sync_desktop.pages import settings_page, sync_page
    from notion_sync_desktop.services import update_check
    from sync_lib import credentials

    credentials.get_token = lambda: ("", "keyring")
    settings_page.credentials.get_token = lambda: ("", "keyring")
    sync_page.get_token = lambda: ("", "keyring")
    update_check.UpdateChecker.start = lambda self: None
    main_window.UpdateChecker.start = lambda self: None


def shoot(win, page, name: str) -> None:
    from PySide6.QtTest import QTest

    win.switchTo(page)
    # 要真的等：切页有平滑滚动，进度条填充是 150ms 的 QPropertyAnimation。
    # 光调 processEvents() 不推进时间，抓到的会是动画中间态。
    QTest.qWait(400)
    win.grab().save(str(OUT / f"{name}.png"))
    print(f"  {name}.png")


def main() -> int:
    _assert_isolated()
    build_demo_folders()
    write_ui_settings()
    write_history()
    _stub_offline()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setTheme

    from notion_sync_desktop.main_window import MainWindow

    # 必须留个引用：这个对象被 GC 掉，后面构造控件就会崩
    app = QApplication(sys.argv)  # noqa: F841
    setTheme(Theme.LIGHT)  # README 在亮色下读
    OUT.mkdir(parents=True, exist_ok=True)

    win = MainWindow()
    if hasattr(win, "setMicaEffectEnabled"):
        win.setMicaEffectEnabled(False)  # 半透明材质抓不下来，会渲成黑块
    win.resize(1080, 720)
    # 走真实平台插件（字体、DPI 都是本机那份），但不上屏：不弹窗打扰人
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.show()

    # 「同步到」那一行靠这份页面列表缓存查标题；不预置它就会显示成一串 id
    win.sync_page._pages = [{"id": PAGE_ID, "title": PAGE_TITLE}]
    win.sync_page.set_folder(str(NOTES))
    win.sync_page.set_busy(True)  # 真实入口：取消按钮现身、其余按钮禁用
    for kind, data in SYNC_EVENTS:
        ev = {"type": kind, "data": data}
        win.history.write(ev)  # 与 MainWindow 里接线的顺序一致：history 先接
        win._on_event(ev)
    win._on_event({"type": "progress", "data": {"done": 6, "total": 7}})

    print("生成截图：")
    shoot(win, win.sync_page, "sync")
    shoot(win, win.plan_page, "plan")

    # 上面喂出来的「正在跑的那一次」也落了盘。收掉它再删，免得历史里多一条没跑完的
    win.history.finish(0, False)
    win.history.path.unlink(missing_ok=True)

    win.history_page.refresh()
    win.history_page.table.selectRow(0)
    win.history_page._on_row_clicked(win.history_page.model.index(0, 0))
    shoot(win, win.history_page, "history")

    shoot(win, win.settings_page, "settings")

    win.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        for path in (NOTES, REPORTS):
            shutil.rmtree(path, ignore_errors=True)
        shutil.rmtree(WORK, ignore_errors=True)
