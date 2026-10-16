"""库入口（设计 v2 §7.4）：编排八步流水线，不调用 `sys.exit`、不依赖 cwd。

与旧 `cli.cmd_sync` 的差别只有三处（红线「核心同步逻辑零改动」）：

- 事件经 `Reporter` 上报，不再直接 `print`；
- 出错返回退出码，不再 `sys.exit`；
- 文件边界与限流等待处响应 `CancelToken`。

`canonical / scanner / remote / planner / plan / executor / state` 的算法未改。
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from notion_client import Client

from .cancel import CancelToken, Cancelled
from .canonical import compute_hash, normalize_path
from .credentials import get_token
from .executor import (
    Summary,
    backup_remote_content,
    execute_plan,
    resolve_conflict,
)
from .lock import LockBusy, RunLock
from .plan import build_plan
from .planner import Action
from .protocol import EXIT, plan_fingerprint, plan_row, side_label
from .remote import NotionRemote
from .reporter import Reporter
from .scanner import scan_local
from .state import StateManager, cleanup_tmp_files

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

# 主循环落盘节奏（1.1）：每处理这么多文件或这么久提交一次状态（先到为准），
# 替代过去逐文件 fsync。强杀最多丢最后这么多文件的基线，下次运行按内容重新
# 分类收敛（两侧同改判 BASELINE，CREATE 的 page_id 另行逐文件落盘，不会重复建页）。
_FLUSH_EVERY_FILES = 20
_FLUSH_EVERY_SECONDS = 2.0

# 引擎所有 Notion 客户端的超时。必须小于桌面端的取消宽限期 CANCEL_GRACE_MS(15s)：
# 取消时引擎至多卡在一次请求上，请求超时后主循环就能看到取消标记并正常收尾，
# 而不是被强杀在半路（M1）。
_HTTP_TIMEOUT_MS = 10_000


class SyncError(Exception):
    """致命错误（配置缺失、无 token、未初始化等）。

    取代旧 CLI 里的 `sys.exit(1)`：库不该结束调用方的进程。
    """

    def __init__(self, message: str, code: str = "fatal"):
        super().__init__(message)
        self.code = code


@dataclass
class RunOptions:
    """一次运行的开关。只放引擎真正会读的项。

    刻意不含 `adopt_remote / paths`：那几项属于设计 §7.5 里尚未实现的命令，
    加了就是死占位符。`path` / `keep` / `backup_only` 只服务于 `resolve`（P3 起真实存在）。
    `only` 是界面的勾选子集：非 None 时只执行集合里的路径（清单由 `--only-file` 读入）。
    """

    dry_run: bool = False
    full: bool = False
    expect_plan: str | None = None
    parent_page_id: str | None = None
    path: str | None = None  # resolve 的目标
    keep: str | None = None  # resolve 的裁决：local / remote / merged
    backup_only: bool = False  # resolve 只落远端副本，不做裁决
    only: frozenset[str] | None = None  # 只执行这些路径（POSIX 相对）；None = 全量

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "full": self.full,
            "expect_plan": self.expect_plan,
            "parent_page_id": self.parent_page_id,
            "path": self.path,
            "keep": self.keep,
            "backup_only": self.backup_only,
            # 只记条数：事件流会被写进运行历史，几百个路径没必要跟着一起落盘
            "only": len(self.only) if self.only is not None else None,
        }


@dataclass
class RunResult:
    exit_code: int
    summary: dict[str, Any]


@dataclass
class _Context:
    root: Path
    config: dict
    state: StateManager
    remote: NotionRemote


def _safe_save(state_mgr: StateManager):
    """异常路径上的尽力落盘。

    save 自身也可能失败（比如 Windows 上 os.replace 撞上占用报 PermissionError）；
    此时吞掉，不能让它顶替正在上浮的原始异常（M1）。
    """
    try:
        state_mgr.save()
    except Exception:
        pass


def run(
    command: str,
    root: Path | str,
    opts: RunOptions,
    rep: Reporter,
    cancel: CancelToken,
) -> RunResult:
    """执行一条命令，返回退出码与摘要；任何情况下都不抛异常给调用方。

    只有 `Cancelled` 与 `LockBusy` 之外的意外异常（如程序缺陷）会继续向上抛，
    由调用方决定怎么报——把它们伪装成正常退出码会掩盖真实故障。
    """
    root = Path(root).resolve()
    started_at = time.monotonic()
    run_started = _now_iso()
    rep.emit(
        "run.started",
        command=command,
        root=str(root),
        options=opts.as_dict(),
    )

    if command == "init":
        return _run_init(root, opts, rep, started_at)

    if command == "pages":
        # 只读查询，跟 init 一样不读配置、不拿运行锁——它只认 token，跟某个文件夹
        # 无关，所以能在同步进行中并发跑（拿锁的话会撞上退出码 75）
        return _run_pages(rep, started_at)

    summary = _empty_summary()
    done = 0
    total = 0

    try:
        with RunLock(root, command):
            ctx = _load_context(root, opts, cancel, rep)

            rep.emit("phase", name="recover")
            recovered = ctx.state.recover_pending()
            if recovered:
                rep.emit(
                    "log",
                    level="info",
                    message=f"发现 {len(recovered)} 个未完成操作，正在恢复…",
                )
            cleanup_tmp_files(root)

            rep.emit("phase", name="scan")
            local_snapshot = scan_local(root, ctx.config)

            rep.emit("phase", name="remote")
            remote_index, remote_paths_trashed = _build_remote_index(
                ctx.remote, ctx.state, ctx.config, local_snapshot, opts.full
            )

            rep.emit("phase", name="plan")
            # 第 5 参必须传，否则回收站集合被静默丢弃
            plan = build_plan(
                local_snapshot,
                remote_index,
                ctx.state.state["files"],
                ctx.config,
                remote_paths_trashed,
            )

            rows, local_hashes, remote_hashes = _plan_rows(
                plan, local_snapshot, remote_index, ctx.state.state["files"]
            )
            fingerprint = plan_fingerprint(plan, local_hashes, remote_hashes)
            rep.emit(
                "plan",
                fingerprint=fingerprint,
                rows=rows,
                counts=_counts(plan),
            )

            if command == "status" or opts.dry_run:
                return _finish(rep, EXIT.OK, summary, started_at)

            if command == "resolve":
                return _run_resolve(
                    ctx, plan, local_snapshot, remote_index, opts, rep, summary, started_at
                )

            if opts.expect_plan and opts.expect_plan != fingerprint:
                rep.emit(
                    "plan.changed", expected=opts.expect_plan, actual=fingerprint
                )
                return _finish(rep, EXIT.PLAN_CHANGED, summary, started_at)

            if command == "push":
                _warn_push_remote_changes(plan, rep)

            filtered = _filter_plan(plan, command)
            if opts.only is not None:
                # 勾选子集：只留被选中的路径。指纹仍按全量计划校验（上面），
                # 所以「预览后内容又变了」照样会在执行前被挡下。
                filtered = {p: c for p, c in filtered.items() if p in opts.only}
            filtered, escape_errors = _check_local_containment(filtered, root)

            # 需要用户处理的计划项：CONFLICT / BLOCKED。注意不能读 summary["conflicts"]
            # ——它来自 execute_plan，而 CONFLICT 被 _filter_plan 挡在执行之外，
            # 永远是 0（旧 CLI 的文本输出因此恒为 conflict=0，改动它会破坏文本兼容）。
            needs_attention = sum(
                1
                for c in plan.values()
                if c.action in (Action.CONFLICT, Action.BLOCKED)
            )

            rep.emit("phase", name="execute")
            for err in escape_errors:
                summary["failed"] += 1
                # 越界项在进入执行前就被拒，因此没有配对的 file.started
                rep.emit(
                    "file.finished",
                    path=err["path"],
                    action="",
                    result="failed",
                    status="rejected",
                    error=err["error"],
                )

            items = sorted(filtered.items())
            total = len(items)
            last_flush_index = 0
            last_flush_at = time.monotonic()
            try:
                for index, (path, classification) in enumerate(items, 1):
                    cancel.check()
                    action = classification.action.name
                    rep.emit(
                        "file.started", path=path, action=action, index=index, total=total
                    )
                    item_summary = execute_plan(
                        {path: classification}, ctx.state, ctx.remote, root, ctx.config
                    )
                    _merge(summary, item_summary)
                    rep.emit(
                        "file.finished",
                        path=path,
                        action=action,
                        result="failed" if item_summary.errors else "ok",
                        status="",
                        error=item_summary.errors[0]["error"] if item_summary.errors else None,
                    )
                    rep.emit("progress", done=index, total=total)
                    done = index

                    # 批量落盘：每 20 个文件或满 2 秒（先到为准）
                    if (
                        index - last_flush_index >= _FLUSH_EVERY_FILES
                        or time.monotonic() - last_flush_at >= _FLUSH_EVERY_SECONDS
                    ):
                        ctx.state.save()
                        last_flush_index = index
                        last_flush_at = time.monotonic()
            except BaseException:
                # 取消 / 管道断开 / 任意异常：已完成的基线尽力落盘再原样上浮。
                # 水位线不在这里更新——它只在循环正常走完后推进（见下）。
                _safe_save(ctx.state)
                raise
            else:
                ctx.state.save()

            if not summary["failed"]:
                # 水位线与待吸收集合只在循环正常走完后更新——取消 / 失败时两者
                # 都保持不变（不能放进 finally）。
                notion = ctx.state.state.setdefault("notion", {})
                files = ctx.state.state["files"]
                notion["remote_dirty"] = sorted(
                    p
                    for p, r in remote_index.items()
                    if r.get("fresh")
                    and p in files
                    and (
                        not r["struct_valid"]
                        or r["hash"] != files[p].get("base_remote_hash")
                    )
                )
                notion["remote_watermark"] = run_started
                ctx.state.save()

        if summary["failed"]:
            code = EXIT.FAILED
        elif needs_attention:
            code = EXIT.NEEDS_ATTENTION
        else:
            code = EXIT.OK
        return _finish(rep, code, summary, started_at, executed=True)

    except LockBusy as e:
        rep.emit("lock.busy", holder=e.holder)
        return RunResult(EXIT.LOCK_BUSY, summary)
    except Cancelled:
        rep.emit(
            "run.cancelled", done=done, remaining=max(total - done, 0)
        )
        return RunResult(EXIT.CANCELLED, summary)
    except SyncError as e:
        rep.emit("warning", code=e.code, message=str(e))
        return _finish(rep, EXIT.FATAL, summary, started_at)


def _run_init(
    root: Path, opts: RunOptions, rep: Reporter, started_at: float
) -> RunResult:
    """初始化：建远端数据库、写配置、写状态。不加运行锁（此时还没有配置）。"""
    summary = _empty_summary()
    try:
        token, _source = get_token()
        if not token:
            # 沿用旧 CLI 的原文，保持文本输出兼容
            raise SyncError("错误：NOTION_TOKEN 未设置", code="no-token")
        if not opts.parent_page_id:
            raise SyncError("init 需要 --parent-page-id", code="no-parent")

        client = Client(auth=token, timeout_ms=_HTTP_TIMEOUT_MS)
        remote = NotionRemote(client, None, None)
        db_id, ds_id = remote.create_database(opts.parent_page_id)

        config = {
            "notion": {
                "parent_page_id": opts.parent_page_id,
                "api_version": "2025-09-03",
            },
            "include": ["/*.md", "docs/**/*.md", "src/**/*.py"],
            "exclude": ["artifacts/", ".*/"],
            "max_chars": 1_000_000,
            "max_payload_kb": 450,
            "default_eol": "crlf",
            "rate_limit_rps": 2.5,
            "max_retries": 6,
            "remote_overlap_minutes": 5,
        }
        (root / ".sync-config.yaml").write_text(
            yaml.dump(config, allow_unicode=True), encoding="utf-8"
        )

        state = StateManager(root)
        state.load()
        state.state["notion"] = {"database_id": db_id, "data_source_id": ds_id}
        state.save()

        rep.emit("log", level="info", message=f"✅ 初始化完成：database_id={db_id}")
    except SyncError as e:
        rep.emit("warning", code=e.code, message=str(e))
        return _finish(rep, EXIT.FATAL, summary, started_at)

    return _finish(rep, EXIT.OK, summary, started_at)


def _run_pages(rep: Reporter, started_at: float) -> RunResult:
    """列出 integration 能访问的页面（只读）。给界面的「选择目标页面」用。"""
    summary = _empty_summary()
    try:
        token, _source = get_token()
        if not token:
            # 沿用旧 CLI 的原文，保持文本输出兼容
            raise SyncError("错误：NOTION_TOKEN 未设置", code="no-token")

        client = Client(auth=token, timeout_ms=_HTTP_TIMEOUT_MS)
        remote = NotionRemote(client, None, None)
        rep.emit("pages", items=remote.search_pages())
    except SyncError as e:
        rep.emit("warning", code=e.code, message=str(e))
        return _finish(rep, EXIT.FATAL, summary, started_at)

    return _finish(rep, EXIT.OK, summary, started_at)


_KEEP_LABELS = {"local": "本地", "remote": "远端", "merged": "合并后的本地"}


def _run_resolve(
    ctx: _Context,
    plan: dict,
    local_snapshot,
    remote_index: dict,
    opts: RunOptions,
    rep: Reporter,
    summary: dict,
    started_at: float,
) -> RunResult:
    """解决一个冲突项（设计 6.5 / 6.6）；`--backup-only` 时只落远端副本。

    先按正常流程重新分类（所以 `resolve` 也会发出完整的 `plan` 事件，界面据此
    刷新计划表），再校验目标确实处于冲突态，最后交给 `resolve_conflict`。
    """
    path = opts.path
    if not path:
        raise SyncError("resolve 需要指定要解决的路径", code="no-path")
    if opts.backup_only and opts.keep:
        raise SyncError("--backup-only 与 --keep 不能同时使用", code="bad-flags")
    if not opts.backup_only and opts.keep not in _KEEP_LABELS:
        raise SyncError("resolve 需要 --keep local|remote|merged", code="no-keep")

    classification = plan.get(path)
    if classification is None:
        raise SyncError(f"{path} 不在当前计划里", code="not-in-plan")
    # 已经解决过、状态仍是 conflict 的项会被分类成 BLOCKED，所以两种都接受；
    # 但 BLOCKED 里还有"远端结构异常"，那种不是冲突，不能放行。
    if (
        classification.action != Action.CONFLICT
        and ctx.state.get_file(path).get("status") != "conflict"
    ):
        raise SyncError(
            f"{path} 当前不是冲突（{classification.action.name}），无需解决",
            code="not-conflict",
        )

    local_exists = path in local_snapshot.files
    remote_exists = path in remote_index
    page_id = ctx.state.get_file(path).get("page_id") or (
        remote_index[path]["page_id"] if remote_exists else None
    )

    if opts.backup_only:
        if not remote_exists:
            raise SyncError(
                f"{path}: 远端没有这个页面，没有可对比的远端副本", code="no-remote"
            )
        return _run_backup_only(ctx, path, page_id, rep, summary, started_at)

    if opts.keep == "merged" and not (local_exists and remote_exists):
        raise SyncError(
            f"{path} 两侧不是都有文件，没有可合并的版本", code="bad-keep"
        )

    rep.emit("phase", name="execute")
    rep.emit("file.started", path=path, action="RESOLVE", index=1, total=1)
    try:
        result = resolve_conflict(
            path,
            opts.keep,
            ctx.state,
            ctx.remote,
            ctx.root,
            local_exists,
            remote_exists,
            page_id,
        )
    except Exception as e:
        summary["failed"] += 1
        rep.emit(
            "file.finished",
            path=path,
            action="RESOLVE",
            result="failed",
            status="",
            error=str(e),
        )
        return _finish(rep, EXIT.FAILED, summary, started_at, executed=True)

    _merge(summary, result.summary)
    rep.emit(
        "file.finished", path=path, action="RESOLVE", result="ok", status="", error=None
    )
    rep.emit("progress", done=1, total=1)

    message = f"已解决：{path}（保留{_KEEP_LABELS[opts.keep]}）"
    if result.backup:
        message += f"，放弃的一侧已备份到 {result.backup}"
    rep.emit("log", level="info", message=message)
    return _finish(rep, EXIT.OK, summary, started_at, executed=True)


def _run_backup_only(ctx, path, page_id, rep, summary, started_at):
    """`--backup-only`：只把远端内容落盘成对比用的副本，不改状态、不做裁决。"""
    rep.emit("phase", name="execute")
    rep.emit("file.started", path=path, action="BACKUP", index=1, total=1)
    try:
        backup = backup_remote_content(path, ctx.remote, ctx.root, page_id)
    except Exception as e:
        summary["failed"] += 1
        rep.emit(
            "file.finished",
            path=path,
            action="BACKUP",
            result="failed",
            status="",
            error=str(e),
        )
        return _finish(rep, EXIT.FAILED, summary, started_at, executed=True)

    rep.emit(
        "file.finished", path=path, action="BACKUP", result="ok", status="", error=None
    )
    rep.emit("progress", done=1, total=1)
    rep.emit(
        "log",
        level="info",
        message=f"远端副本已写入 {backup.relative_to(ctx.root).as_posix()}",
    )
    return _finish(rep, EXIT.OK, summary, started_at, executed=True)


def _load_context(
    root: Path, opts: RunOptions, cancel: CancelToken, rep: Reporter
) -> _Context:
    """读配置、取 token、开状态、建远端客户端。缺任何一项都抛 `SyncError`。"""
    config_path = root / ".sync-config.yaml"
    if not config_path.exists():
        raise SyncError(
            f"未找到 {config_path}，请先运行 notion-sync init", code="no-config"
        )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    token, _source = get_token()
    if not token:
        raise SyncError("错误：NOTION_TOKEN 未设置", code="no-token")

    state = StateManager(root)
    state.load()

    notion = state.state.get("notion", {})
    if not notion.get("data_source_id"):
        raise SyncError(
            "状态里没有 data_source_id，请先运行 notion-sync init", code="no-state"
        )

    client = Client(auth=token, timeout_ms=_HTTP_TIMEOUT_MS)
    remote = NotionRemote(
        client,
        notion.get("database_id"),
        notion.get("data_source_id"),
        cancel=cancel,
    )
    return _Context(root, config, state, remote)


def _build_remote_index(remote, state_mgr, config, local_snapshot, full):
    """步骤 4：构建 remote_index（设计 5.4）。

    - `--full`：对 state.files 里每个有 page_id 的 path 精判（skip 粗筛）。
    - 否则粗筛：query_candidates(watermark − overlap)，对候选逐个 read_page 精判。
    - 非候选的已跟踪页面视为 R == BR。
    - 远端在回收站的 path 收进 remote_paths_trashed。
    - local 有但 remote_index 没有的 path，用 query_by_paths 探测 ADOPT 场景。
    - 待吸收集合（notion.remote_dirty）里的 path 强制进候选，并标记 fresh。

    返回 (remote_index, remote_paths_trashed)。
    """
    state_files = state_mgr.state["files"]
    notion = state_mgr.state.setdefault("notion", {})
    watermark = notion.get("remote_watermark")

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

    # 待吸收集合：上一轮远端有变更、但没被 PULL 吸收（勾选子集 / push / CONFLICT）
    # 的页面。水位线推进后它们不再是候选，必须在这里强制读回，否则远端修改会被
    # 永久漏检（B2）。
    cand_ids = {c["id"] for c in candidates}
    for path in notion.get("remote_dirty", []):
        entry = state_files.get(path)
        if not entry or not entry.get("page_id") or entry["page_id"] in cand_ids:
            continue
        candidates.append(
            {
                "id": entry["page_id"],
                "path": path,
                "last_edited_time": entry.get("remote_edited_at"),
                "in_trash": False,
            }
        )

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
            "fresh": True,  # 本轮真实读过，dirty 集合只认这些条目
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
                "fresh": True,
            }

    return remote_index, remote_paths_trashed


def _plan_rows(plan, local_snapshot, remote_index, state_files):
    """把 plan 序列化成协议行，并顺带取出指纹要用的 L / R 哈希。

    `plan.py` 里的 `Classification` 不含内容哈希，所以 L / R 只能在这里从
    本地快照和远端索引取——这也是不改 `plan.py` 就能给出 §3.5 所需指纹的前提。
    """
    rows = []
    local_hashes = {}
    remote_hashes = {}
    for path in sorted(plan):
        entry = state_files.get(path, {})
        l_exists = path in local_snapshot.files
        l_hash = local_snapshot.files[path]["hash"] if l_exists else None
        r_exists = path in remote_index
        r_hash = remote_index[path]["hash"] if r_exists else None

        local_hashes[path] = l_hash
        remote_hashes[path] = r_hash
        rows.append(
            plan_row(
                path,
                plan[path],
                side_label(l_exists, l_hash, entry.get("base_local_hash")),
                side_label(r_exists, r_hash, entry.get("base_remote_hash")),
            )
        )
    return rows, local_hashes, remote_hashes


def _counts(plan) -> dict[str, int]:
    """动作 → 条数，供 UI 直接显示，免得它自己再扫一遍 rows。"""
    counts: dict[str, int] = {}
    for classification in plan.values():
        name = classification.action.name
        counts[name] = counts.get(name, 0) + 1
    return counts


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


def _warn_push_remote_changes(plan, rep: Reporter):
    """push 计划含 PULL/CONFLICT → 警告远端有修改。"""
    if any(c.action in (Action.PULL, Action.CONFLICT) for c in plan.values()):
        rep.emit("warning", code="remote-changes", message="警告：远端有修改，请先 pull 或 sync")


def _empty_summary() -> dict[str, Any]:
    return {
        "pushed": 0,
        "pulled": 0,
        "created": 0,
        "adopted": 0,
        "archived": 0,
        "deleted": 0,
        "renamed": 0,
        "baselined": 0,
        "conflicts": 0,
        "skipped": 0,
        "failed": 0,
    }


def _merge(dst: dict, src: Summary) -> None:
    dst["pushed"] += src.pushed
    dst["pulled"] += src.pulled
    dst["created"] += src.created
    dst["adopted"] += src.adopted
    dst["archived"] += src.archived
    dst["deleted"] += src.deleted
    dst["renamed"] += src.renamed
    dst["baselined"] += src.baselined
    dst["conflicts"] += src.conflicts
    dst["skipped"] += src.skipped
    dst["failed"] += len(src.errors or [])


def _finish(
    rep: Reporter,
    code: int,
    summary: dict,
    started_at: float,
    executed: bool = False,
) -> RunResult:
    """收尾事件。

    `executed` 只有真正跑过执行阶段才为 True。旧 CLI 在执行阶段之前就 return 的
    分支（status / --dry-run / init）不打印摘要，`ConsoleReporter` 靠这个标志
    决定要不要输出摘要行——否则文本输出会多出一行。
    """
    rep.emit(
        "run.finished",
        exit_code=code,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        executed=executed,
        summary=summary,
    )
    return RunResult(code, summary)


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
