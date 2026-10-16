"""构建同步计划：把本地快照、远端索引与状态三方合并为逐路径分类（纯函数）。

对应设计 6.1 步骤 5 与 6.2/6.3/6.4 三张分类表。本模块不触碰 remote/IO：
remote_index 由调用方（cmd_sync 编排）预先拉取后传入。
"""

from .canonical import normalize_path
from .planner import (
    Action,
    Classification,
    classify_existence,
    classify_file,
    detect_renames,
)
from .scanner import compile_scan_rules, in_scope


def _normalize_files(mapping):
    """把 {path: entry} 的键统一为 NFC POSIX 形式（防御调用方传入未规范化的键）。"""
    return {normalize_path(path): entry for path, entry in mapping.items()}


def build_plan(local_snapshot, remote_index, state, config, remote_paths_trashed=None):
    """合并三方视角，产出 {path: Classification}。

    参数：
        local_snapshot: scanner.LocalSnapshot，其 ``.files`` 为 path → {hash, ...}。
        remote_index:   {path: {page_id, hash, last_edited_time, struct_valid}}。
        state:          state["files"] 映射（path → 条目），含 base_local_hash /
                        base_remote_hash / page_id / status。
        config:         配置字典；include/exclude（gitwildmatch）+ 内置排除共同决定
                        「路径是否归同步管」——不在范围内且 state 有记录的判 out-of-scope。
        remote_paths_trashed: 远端在回收站的 path 集合（视为远端不存在）。

    返回 {path: Classification}。除 SKIP / UNTRACKED 外每项均可执行；reason 供计划表展示。
    """
    local_files = _normalize_files(local_snapshot.files)
    remote_files = _normalize_files(remote_index)
    state_files = _normalize_files(state)
    trashed = {normalize_path(p) for p in (remote_paths_trashed or set())}

    rules = compile_scan_rules(config)

    # 远端已修改的 path（远端 hash 与基线不同）→ rename 检测需跳过
    remote_changed = {
        path
        for path, entry in state_files.items()
        if path in remote_files
        and remote_files[path]["hash"] != entry.get("base_remote_hash")
    }

    # 1. 重命名检测先行（6.4）：old_path 产 RENAME，new_path 不再单独分类
    renames = detect_renames(state_files, local_files, remote_changed)
    rename_old = {r.old_path: r for r in renames}
    rename_new = {r.new_path for r in renames}

    plan = {}
    paths = set(local_files) | set(state_files) | set(remote_files)
    for path in paths:
        if path in rename_new:
            continue
        if path in rename_old:
            r = rename_old[path]
            plan[path] = Classification(
                Action.RENAME,
                "rename",
                {"new_path": r.new_path, "page_id": r.page_id},
            )
            continue

        entry = state_files.get(path)
        in_state = entry is not None

        if path in local_files:
            local_exists = True
            local_hash = local_files[path]["hash"]
        elif in_state and not in_scope(path, rules):
            # 「不在扫描范围内」与「用户删了」是两回事：缩小 include / 改 exclude /
            # 命中内置排除时，文件只是不再归同步管，据此归档远端页会一次清掉一片
            # （M6，设计 4.2）。真要移除远端请用 forget。
            local_exists = "out-of-scope"
            local_hash = None
        else:
            local_exists = False
            local_hash = None

        remote_exists = path in remote_files and path not in trashed
        remote_hash = remote_files[path]["hash"] if remote_exists else None

        if in_state and local_exists is True and remote_exists:
            # 2. 两端都存在的已跟踪文件（6.2）
            classification = classify_file(
                entry,
                local_hash,
                remote_hash,
                remote_exists=True,
                remote_struct_valid=remote_files[path]["struct_valid"],
            )
        else:
            # 3. 存在性分类（6.3）
            classification = classify_existence(
                in_state,
                local_exists,
                remote_exists,
                local_hash,
                remote_hash,
                base_remote_hash=entry.get("base_remote_hash") if in_state else None,
                base_local_hash=entry.get("base_local_hash") if in_state else None,
            )
            # 4. ADOPT 附带 page_id，供 executor 建立基线
            if classification.action == Action.ADOPT:
                classification.meta = {"page_id": remote_files[path]["page_id"]}

        plan[path] = classification

    return plan
