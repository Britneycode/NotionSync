import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from notion_client import Client

from .canonical import compute_hash, normalize_path
from .executor import execute_plan
from .plan import build_plan
from .planner import Action
from .remote import NotionRemote
from .scanner import scan_local
from .state import StateManager, acquire_lock, cleanup_tmp_files, release_lock

# push/pull 各自允许执行的动作（设计 6.1 表）
_PUSH_ACTIONS = {
    Action.CREATE_REMOTE,
    Action.PUSH,
    Action.ARCHIVE_REMOTE,
    Action.RENAME,
    Action.ADOPT,
}
_PULL_ACTIONS = {Action.PULL, Action.DELETE_LOCAL, Action.ADOPT}

# 不可执行的动作：仅报告或无动作（CONFLICT 始终只报告）
_NON_EXECUTABLE = {
    Action.SKIP,
    Action.BLOCKED,
    Action.CONFLICT,
    Action.UNMANAGED,
    Action.UNTRACKED,
    Action.DELETE_STATE,
}


def main():
    # Windows GBK 控制台无法输出 emoji/中文，重配为 UTF-8
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Notion 双向同步")
    subparsers = parser.add_subparsers(dest="command")

    # init
    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--parent-page-id", required=True)

    # status/push/pull/sync
    for cmd in ["status", "push", "pull", "sync"]:
        p = subparsers.add_parser(cmd)
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--full", action="store_true")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command in ("status", "push", "pull", "sync"):
        cmd_sync(args)

def cmd_init(args):
    """初始化"""
    root = Path.cwd()
    token = os.getenv("NOTION_TOKEN")
    if not token:
        print("错误：NOTION_TOKEN 未设置")
        sys.exit(1)

    # 创建数据库
    client = Client(auth=token)
    remote = NotionRemote(client, None, None)
    db_id, ds_id = remote.create_database(args.parent_page_id)

    # 生成配置
    config = {
        "notion": {"parent_page_id": args.parent_page_id, "api_version": "2025-09-03"},
        "include": ["/*.md", "docs/**/*.md", "src/**/*.py"],
        "exclude": ["artifacts/", ".*/"],
        "max_chars": 150000,
        "max_payload_kb": 450,
        "default_eol": "crlf",
        "rate_limit_rps": 2.5,
        "max_retries": 6,
        "remote_overlap_minutes": 5,
    }
    (root / ".sync-config.yaml").write_text(yaml.dump(config, allow_unicode=True))

    # 初始化状态
    state_mgr = StateManager(root)
    state_mgr.load()
    state_mgr.state["notion"] = {"database_id": db_id, "data_source_id": ds_id}
    state_mgr.save()

    print(f"✅ 初始化完成：database_id={db_id}")

def cmd_sync(args):
    """同步流水线（设计 6.1 八步）。status 等价于 sync --dry-run（只读）。"""
    root = Path.cwd()

    # 1. 获取进程锁
    if not acquire_lock(root):
        print("错误：另一个同步进程正在运行")
        sys.exit(1)

    try:
        run_started = _now_iso()

        # 加载配置和状态
        config = yaml.safe_load((root / ".sync-config.yaml").read_text())
        state_mgr = StateManager(root)
        state_mgr.load()

        # 2. 恢复 pending + 清理临时文件
        recovered = state_mgr.recover_pending()
        if recovered:
            print(f"发现 {len(recovered)} 个未完成操作，正在恢复…")
        cleanup_tmp_files(root)

        # 3. 扫描本地
        local_snapshot = scan_local(root, config)

        # 远端客户端
        token = os.getenv("NOTION_TOKEN")
        client = Client(auth=token)
        notion = state_mgr.state.get("notion", {})
        remote = NotionRemote(
            client, notion.get("database_id"), notion.get("data_source_id")
        )

        # 4. 构建 remote_index（粗筛 + 精判）
        remote_index, remote_paths_trashed = _build_remote_index(
            remote, state_mgr, config, local_snapshot, args.full
        )

        # 5. 三方合并（第 5 参必须传，否则回收站集合被静默丢弃）
        plan = build_plan(
            local_snapshot,
            remote_index,
            state_mgr.state["files"],
            config,
            remote_paths_trashed,
        )

        # 6. 打印计划表；--dry-run / status 到此结束
        _print_plan(plan)
        if args.dry_run or args.command == "status":
            return

        # 7. 按命令过滤动作 + 写前本地路径校验后执行
        if args.command == "push":
            _warn_push_remote_changes(plan)
        filtered = _filter_plan(plan, args.command)
        filtered, escape_errors = _check_local_containment(filtered, root)
        summary = execute_plan(filtered, state_mgr, remote, root, config)
        summary.errors.extend(escape_errors)

        # 8. 输出摘要；无 errors 才推进水位线
        _print_summary(summary)
        if not summary.errors:
            state_mgr.state.setdefault("notion", {})["remote_watermark"] = run_started
            state_mgr.save()

    finally:
        release_lock(root)


def _build_remote_index(remote, state_mgr, config, local_snapshot, full):
    """步骤 4：构建 remote_index（设计 5.4）。

    - `--full`：对 state.files 里每个有 page_id 的 path 精判（skip 粗筛）。
    - 否则粗筛：query_candidates(watermark − overlap)，对候选逐个 read_page 精判。
    - 非候选的已跟踪页面视为 R == BR。
    - 远端在回收站的 path 收进 remote_paths_trashed。
    - local 有但 remote_index 没有的 path，用 query_by_paths 探测 ADOPT 场景。

    返回 (remote_index, remote_paths_trashed)。
    """
    state_files = state_mgr.state["files"]
    watermark = state_mgr.state.get("notion", {}).get("remote_watermark")

    remote_index = {}
    remote_paths_trashed = set()

    if full:
        candidates = [
            {
                "id": entry["page_id"],
                "path": path,
                "last_edited_time": entry.get("remote_edited_at"),
                "in_trash": False,
            }
            for path, entry in state_files.items()
            if entry.get("page_id")
        ]
    else:
        candidates = remote.query_candidates(_watermark_since(watermark, config))

    for cand in candidates:
        path = cand.get("path")
        if not path:
            continue
        path = normalize_path(path)
        if cand.get("in_trash"):
            remote_paths_trashed.add(path)
            continue
        content, _block_id, valid = remote.read_page(cand["id"])
        remote_index[path] = {
            "page_id": cand["id"],
            "hash": compute_hash(content) if valid else None,
            "struct_valid": valid,
            "last_edited_time": cand.get("last_edited_time"),
        }
        entry = state_files.get(path)
        if entry is not None:
            entry["remote_edited_at"] = cand.get("last_edited_time")

    # 非候选的已跟踪页面：视为 R == BR
    for path, entry in state_files.items():
        if path in remote_index or path in remote_paths_trashed:
            continue
        if entry.get("page_id"):
            remote_index[path] = {
                "page_id": entry["page_id"],
                "hash": entry.get("base_remote_hash"),
                "struct_valid": True,
                "last_edited_time": entry.get("remote_edited_at"),
            }

    # ADOPT 探测：local 有但 remote_index 没有的 path
    probe = [
        path
        for path in local_snapshot.files
        if path not in remote_index and path not in remote_paths_trashed
    ]
    if probe:
        for path, page_id in remote.query_by_paths(probe).items():
            content, _block_id, valid = remote.read_page(page_id)
            remote_index[path] = {
                "page_id": page_id,
                "hash": compute_hash(content) if valid else None,
                "struct_valid": valid,
                "last_edited_time": None,
            }

    return remote_index, remote_paths_trashed


def _filter_plan(plan, command):
    """步骤 7：按命令过滤动作（设计 6.1 表）。

    CONFLICT 始终只报告（不进执行）；BASELINE 始终执行；push/pull 用各自白名单，
    sync 执行全部可执行动作。
    """
    if command == "push":
        allowed = _PUSH_ACTIONS
    elif command == "pull":
        allowed = _PULL_ACTIONS
    else:
        allowed = None  # sync：全部可执行动作

    filtered = {}
    for path, classification in plan.items():
        action = classification.action
        if action in _NON_EXECUTABLE:
            continue
        if action == Action.BASELINE or allowed is None or action in allowed:
            filtered[path] = classification
    return filtered


def _check_local_containment(plan, root):
    """PULL/DELETE_LOCAL 写本地前：(root/path).resolve() 必须在 root 内，防符号链接逃逸。

    返回 (safe_plan, errors)：越界项从计划中剔除并记入 errors。
    """
    root_resolved = Path(root).resolve()
    safe = {}
    errors = []
    for path, classification in plan.items():
        if classification.action in (Action.PULL, Action.DELETE_LOCAL):
            target = (Path(root) / path).resolve()
            if not target.is_relative_to(root_resolved):
                errors.append({"path": path, "error": "path escapes root"})
                continue
        safe[path] = classification
    return safe, errors


def _warn_push_remote_changes(plan):
    """push 计划含 PULL/CONFLICT → 警告远端有修改。"""
    if any(c.action in (Action.PULL, Action.CONFLICT) for c in plan.values()):
        print("警告：远端有修改，请先 pull 或 sync")


def _print_plan(plan):
    """打印计划表。"""
    if not plan:
        print("计划为空，无需同步")
        return
    print("同步计划：")
    for path in sorted(plan):
        classification = plan[path]
        line = f"  {classification.action.value:<14} {path}"
        if classification.reason:
            line += f"  ({classification.reason})"
        print(line)


def _print_summary(summary):
    """打印摘要。"""
    print(
        f"完成：push={summary.pushed} pull={summary.pulled} "
        f"create={summary.created} adopt={summary.adopted} "
        f"archive={summary.archived} delete={summary.deleted} "
        f"rename={summary.renamed} baseline={summary.baselined} "
        f"conflict={summary.conflicts} skip={summary.skipped} "
        f"errors={len(summary.errors)}"
    )


def _watermark_since(watermark, config):
    """水位线 − overlap → since_iso（None 表示全量）。"""
    if watermark is None:
        return None
    try:
        dt = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
    except ValueError:
        return None
    dt -= timedelta(minutes=config.get("remote_overlap_minutes", 5))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso():
    """当前 UTC 时间（ISO 8601，Z 结尾）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
