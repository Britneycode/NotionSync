"""命令行拼装自检：python ui/test_app.py

只验证 build_args —— 界面与 sync.py 之间唯一的翻译层，最容易悄悄错。
真正的端到端验证是界面里点一次「试运行」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import build_args  # noqa: E402

HERE = str(Path(__file__).resolve().parent)   # 拿一个真实存在的目录当目标

# 普通命令 + 试运行 → 追加 --dry-run
_, args, err = build_args(HERE, "push", dry_run=True)
assert err is None and args[2] == "push" and args[-1] == "--dry-run", (args, err)

# 普通命令不带试运行 → 不追加
_, args, err = build_args(HERE, "sync")
assert err is None and "--dry-run" not in args, (args, err)

# init 走 --parent-page-id，且不吃 --dry-run
_, args, err = build_args(HERE, "init", dry_run=True, parent_page_id="abc123")
assert err is None and args[-2:] == ["--parent-page-id", "abc123"], (args, err)
assert "--dry-run" not in args, args

# init 缺父页面 ID → 必须拦下
_, _, err = build_args(HERE, "init")
assert err is not None, "init 缺 parent_page_id 竟然放行了"

# 未知命令 → 拦下
_, _, err = build_args(HERE, "bogus")
assert err is not None

# 目录不存在 → 拦下
_, _, err = build_args("D:/definitely/not/a/real/folder", "push")
assert err is not None

print("build_args 自检通过（6 项）")
