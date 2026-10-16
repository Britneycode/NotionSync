"""执行同步计划：各动作处理器与乐观并发写前校验（设计文档 5.5 / 6.6 节）。"""

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .canonical import canonical, compute_hash, detect_format, restore_format
from .planner import Action, Rename

logger = logging.getLogger(__name__)

# 扩展名 → Notion Type select 取值（设计 4.2：markdown/python/yaml/toml/text）
_FILE_TYPES = {
    ".md": "markdown",
    ".py": "python",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
}


@dataclass
class Summary:
    pushed: int = 0
    pulled: int = 0
    created: int = 0
    adopted: int = 0
    archived: int = 0
    deleted: int = 0
    renamed: int = 0
    baselined: int = 0
    conflicts: int = 0
    skipped: int = 0
    errors: list = None


def execute_plan(plan, state_mgr, remote, local_root, config):
    """执行同步计划"""
    summary = Summary(errors=[])
    local_root = Path(local_root)

    for path, classification in plan.items():
        action = classification.action

        try:
            if action == Action.PUSH:
                _execute_push(path, state_mgr, remote, local_root)
                summary.pushed += 1
            elif action == Action.PULL:
                _execute_pull(path, state_mgr, remote, local_root)
                summary.pulled += 1
            elif action == Action.CREATE_REMOTE:
                _execute_create(path, state_mgr, remote, local_root)
                summary.created += 1
            elif action == Action.ADOPT:
                _execute_adopt(path, state_mgr, remote, local_root, classification.meta)
                summary.adopted += 1
            elif action == Action.ARCHIVE_REMOTE:
                _execute_archive_remote(path, state_mgr, remote)
                summary.archived += 1
            elif action == Action.DELETE_LOCAL:
                _execute_delete_local(path, state_mgr, local_root)
                summary.deleted += 1
            elif action == Action.RENAME:
                meta = classification.meta or {}
                rename = Rename(path, meta.get("new_path"), meta.get("page_id"))
                _execute_rename(rename, state_mgr, remote, local_root, config)
                summary.renamed += 1
            elif action == Action.BASELINE:
                _execute_baseline(path, state_mgr, remote)
                summary.baselined += 1
            elif action == Action.CONFLICT:
                _execute_conflict(path, state_mgr, remote, local_root)
                summary.conflicts += 1
            else:
                summary.skipped += 1
        except Exception as e:
            summary.errors.append({"path": path, "error": str(e)})

    return summary


def _br_from_update_response(resp, block_id, sent_content):
    """设计 5.5：从 blocks.update 响应体重算 BR。

    校验响应 `type == "code"`、`id == block_id`、`code.rich_text` 为 list 且每项
    含 `plain_text`；任一不满足返回 None（调用方回退为读回远端）。
    """
    if not isinstance(resp, dict):
        return None
    if resp.get("type") != "code":
        return None
    if resp.get("id") != block_id:
        return None
    rich_text = resp.get("code", {}).get("rich_text")
    if not isinstance(rich_text, list):
        return None

    parts = []
    for item in rich_text:
        if not isinstance(item, dict) or "plain_text" not in item:
            return None
        parts.append(item["plain_text"])

    return compute_hash(canonical("".join(parts)))


def _execute_push(path, state_mgr, remote, local_root):
    """执行 PUSH（6.6 写前校验 + 5.5 写响应重算 BR）"""
    content = (local_root / path).read_text(encoding="utf-8-sig")
    content_hash = compute_hash(content)

    # 1. 写意图
    state_mgr.set_pending(path, "push", content_hash)
    entry = state_mgr.get_file(path)

    # 2. 写前校验（6.6）：结构异常 / 远端已改 → 中止并计入 errors。
    #    B1：这份列举结果直接透传给第 3 步，省掉背靠背的第二次整页列举。
    blocks, current_remote_content, valid = remote.read_blocks(entry["page_id"])
    if not valid:
        raise Exception("remote invalid-structure")
    if compute_hash(current_remote_content) != entry["base_remote_hash"]:
        raise Exception("Remote changed during push")

    # 3. 执行更新（正文超单块容量时由远端拆/并 code block）；
    #    BR 优先取写响应体（5.5），否则读回，最后本地 hash 兜底
    resp = remote.update_page_content(entry["page_id"], content, existing=blocks)
    base_remote_hash = _br_from_update_response(resp, entry["block_id"], content)
    if base_remote_hash is None:
        read_back, _, read_valid = remote.read_page(entry["page_id"])
        if read_valid:
            base_remote_hash = compute_hash(read_back)
        else:
            logger.warning("push %s: 写响应与读回均不可用，BR 回退为本地 hash", path)
            base_remote_hash = content_hash

    # 4. 更新状态
    state_mgr.update_file(path, {
        "base_local_hash": content_hash,
        "base_remote_hash": base_remote_hash,
    })
    state_mgr.clear_pending(path)


def _execute_pull(path, state_mgr, remote, local_root):
    """执行 PULL"""
    entry = state_mgr.get_file(path)

    # 1. 写意图
    remote_content, block_id, valid = remote.read_page(entry["page_id"])
    remote_hash = compute_hash(remote_content)
    state_mgr.set_pending(path, "pull", remote_hash)

    # 2. 写入本地
    fmt = entry["format"]
    raw = restore_format(remote_content, fmt)
    local_path = local_root / path
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(raw)

    # 3. 更新状态
    state_mgr.update_file(path, {
        "base_local_hash": remote_hash,
        "base_remote_hash": remote_hash,
        "block_id": block_id,
    })
    state_mgr.clear_pending(path)


def _execute_create(path, state_mgr, remote, local_root):
    """执行 CREATE_REMOTE：本地新建文件 → 远端建页 → 建立基线"""
    raw = (local_root / path).read_bytes()
    content = raw.decode("utf-8-sig")
    content_hash = compute_hash(content)
    file_type = _file_type_from_path(path)
    directory = _directory_from_path(path)
    chars = len(canonical(content).encode("utf-16-le")) // 2

    # 1. 建页；page_id 一返回立刻写进状态并提交（设计 7.2）
    page_id = remote.create_page(path, content, file_type, directory, chars)
    state_mgr.update_file(path, {"page_id": page_id})
    state_mgr.save()

    # 2. 读回取 block_id 与 BR（create 响应不含子块，仍需读回，设计 5.5）
    read_back, block_id, valid = remote.read_page(page_id)
    base_remote_hash = compute_hash(read_back) if valid else content_hash

    # 3. 建立基线
    state_mgr.update_file(path, {
        "block_id": block_id,
        "base_local_hash": content_hash,
        "base_remote_hash": base_remote_hash,
        "format": detect_format(raw),
        "status": "synced",
    })
    state_mgr.save()


def _execute_adopt(path, state_mgr, remote, local_root, meta):
    """执行 ADOPT：state 无记录但远端已有 page_id，建立基线"""
    page_id = (meta or {}).get("page_id")
    if not page_id:
        raise Exception("adopt requires page_id in classification.meta")

    raw = (local_root / path).read_bytes()
    content = raw.decode("utf-8-sig")
    local_hash = compute_hash(content)

    read_back, block_id, valid = remote.read_page(page_id)
    if not valid:
        raise Exception("remote invalid-structure")
    remote_hash = compute_hash(read_back)
    if remote_hash != local_hash:
        # 内容不等应由 planner 分类成 CONFLICT；被直接调用时报错
        raise Exception("adopt content mismatch (expected CONFLICT)")

    state_mgr.update_file(path, {
        "page_id": page_id,
        "block_id": block_id,
        "base_local_hash": local_hash,
        "base_remote_hash": remote_hash,
        "format": detect_format(raw),
        "status": "synced",
    })
    state_mgr.save()


def _execute_archive_remote(path, state_mgr, remote):
    """执行 ARCHIVE_REMOTE：本地删除、远端未改 → 归档远端页并写 tombstone"""
    entry = state_mgr.get_file(path)

    # 乐观校验（6.6）：重读 R == BR
    current, _, valid = remote.read_page(entry["page_id"])
    if not valid:
        raise Exception("remote invalid-structure")
    if compute_hash(current) != entry["base_remote_hash"]:
        raise Exception("Remote changed during archive")

    remote.archive_page(entry["page_id"])
    _add_tombstone(state_mgr, path, entry.get("page_id"), "remote")
    del state_mgr.state["files"][path]
    state_mgr.save()


def forget_file(path, state_mgr, remote, local_root, force=False):
    """停止跟踪一个文件：备份远端正文 → 归档远端页 → tombstone → 清 state 条目。

    对应设计 4.2「规则变化只是不再跟踪，真要移除远端就 forget」。和 ARCHIVE_REMOTE
    一样先做写前校验（6.6）：远端自基线后又有人改过就拒绝，除非显式 force——
    那时也照样先把当前远端正文备份下来（M6）。本地文件不动。
    """
    entry = state_mgr.get_file(path)
    if not entry or not entry.get("page_id"):
        raise Exception(f"{path} 不在跟踪状态里")

    content, _block_id, valid = remote.read_page(entry["page_id"])
    if not valid:
        raise Exception("remote invalid-structure")
    if compute_hash(content) != entry.get("base_remote_hash") and not force:
        raise Exception(
            "Remote changed since baseline：先 pull / resolve 对齐，"
            "或加 --force 强制忘记（会先把远端正文备份下来）"
        )

    backup = _write_remote_backup(path, content, local_root, subdir="forgotten")
    remote.archive_page(entry["page_id"])
    _add_tombstone(state_mgr, path, entry.get("page_id"), "remote")
    del state_mgr.state["files"][path]
    state_mgr.save()
    return backup


def _execute_delete_local(path, state_mgr, local_root):
    """执行 DELETE_LOCAL：远端已删、本地未改 → 本地文件移入备份并写 tombstone"""
    src = local_root / path
    dst = local_root / ".sync-backup" / "deleted" / path
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))

    entry = state_mgr.get_file(path)
    _add_tombstone(state_mgr, path, entry.get("page_id"), "local")
    del state_mgr.state["files"][path]
    state_mgr.save()


def _execute_rename(rename, state_mgr, remote, local_root, config):
    """执行 RENAME：更新远端属性，状态键 old → new"""
    directory = _directory_from_path(rename.new_path)
    properties = {
        "Name": {"title": [{"text": {"content": rename.new_path}}]},
        "Path": {"rich_text": [{"text": {"content": rename.new_path}}]},
        "Directory": {"rich_text": [{"text": {"content": directory}}]},
    }
    remote.update_page_properties(rename.page_id, properties)

    files = state_mgr.state["files"]
    files[rename.new_path] = files.pop(rename.old_path)
    state_mgr.save()


def _execute_baseline(path, state_mgr, remote):
    """执行 BASELINE：两端内容一致，仅把 BL = BR 更新为当前 hash"""
    entry = state_mgr.get_file(path)
    content, _, valid = remote.read_page(entry["page_id"])
    if not valid:
        raise Exception("remote invalid-structure")

    current_hash = compute_hash(content)
    state_mgr.update_file(path, {
        "base_local_hash": current_hash,
        "base_remote_hash": current_hash,
        "status": "synced",
    })
    state_mgr.save()


def _execute_conflict(path, state_mgr, remote, local_root):
    """执行 CONFLICT：远端内容备份到本地，标 conflict；本地不动（设计 6.5）"""
    entry = state_mgr.get_file(path)
    content, _, _ = remote.read_page(entry["page_id"])
    _write_remote_backup(path, content, local_root)

    state_mgr.update_file(path, {"status": "conflict"})
    state_mgr.save()


@dataclass
class ResolveResult:
    """一次 `resolve` 的结果，供调用方汇总与提示。"""

    summary: Summary
    backup: str | None  # 被放弃那一侧的备份文件（相对 root）；没有备份则 None


def resolve_conflict(
    path,
    keep,
    state_mgr,
    remote,
    local_root,
    local_exists,
    remote_exists,
    page_id,
):
    """按用户裁决解决一个冲突项（设计 6.5 / 6.6）。

    刻意**不新增同步算法**：先把该文件的基线重新定到"用户选中的那一侧"，
    再交给既有的 push / pull / archive_remote / delete_local / create 原语执行。
    冲突解决与日常同步因此共用同一套写盘、备份与乐观校验代码。

    四种冲突形态（planner 都会产出 CONFLICT）各自的 `keep` 含义：

    ==================  ====================  ==================  ==========
    形态                keep=local            keep=remote         keep=merged
    ==================  ====================  ==================  ==========
    两端都改            备份远端后推本地      备份本地后拉远端    同 local
    未跟踪但远端有同名页 备份远端后推本地      备份本地后拉远端    同 local
    本地删、远端改       确认删除（归档远端）  拉回本地（重建）    不适用，报错
    远端删、本地改       重建远端页            确认删除（删本地）  不适用，报错
    ==================  ====================  ==================  ==========

    `keep=merged` 表示用户已把合并结果写进了本地文件，因此与 `local` 同路。
    """
    if keep not in ("local", "remote", "merged"):
        raise ValueError(f"unknown keep: {keep!r}（应为 local / remote / merged）")
    if not local_exists and not remote_exists:
        raise ValueError(f"{path}: 两侧都不存在，不是冲突")

    local_root = Path(local_root)
    summary = Summary(errors=[])
    backup = None

    if local_exists and remote_exists:
        if keep == "remote":
            backup = _backup_local(path, local_root)
            _seed_state(path, state_mgr, local_root, page_id)
            _execute_pull(path, state_mgr, remote, local_root)
            summary.pulled += 1
        else:
            # 只读一次远端：备份与重新定基线都用它。6.6 的竞态窗口从这次读取开始算，
            # 之后 _execute_push 会再读一次并比对，窗口内又变则中止。
            content, block_id, valid = remote.read_page(page_id)
            if not valid:
                raise Exception("remote invalid-structure")
            backup = _write_remote_backup(path, content, local_root)
            _rebase_remote(path, state_mgr, local_root, page_id, block_id, content)
            _execute_push(path, state_mgr, remote, local_root)
            summary.pushed += 1

    elif remote_exists:
        # 本地没了、远端还在
        if keep == "merged":
            raise ValueError(f"{path}: 本地已删除，没有可合并的本地版本")
        if keep == "remote":
            _seed_state(path, state_mgr, local_root, page_id)
            _execute_pull(path, state_mgr, remote, local_root)
            summary.pulled += 1
        else:
            content, _, valid = remote.read_page(page_id)
            if not valid:
                raise Exception("remote invalid-structure")
            backup = _write_remote_backup(path, content, local_root)
            _rebase_remote(path, state_mgr, local_root, page_id, None, content)
            _execute_archive_remote(path, state_mgr, remote)
            summary.archived += 1

    else:
        # 远端没了、本地还在
        if keep == "merged":
            raise ValueError(f"{path}: 远端已删除，没有可合并的远端版本")
        if keep == "remote":
            # _execute_delete_local 自己会把本地文件移进 .sync-backup/deleted/
            _execute_delete_local(path, state_mgr, local_root)
            summary.deleted += 1
        else:
            _execute_create(path, state_mgr, remote, local_root)
            summary.created += 1

    # 归档远端 / 删本地这两条路会直接把状态条目删掉（改由 tombstone 记录），
    # 不能再往里写 status——那会把一个已经删掉的条目复活成没有哈希的空壳。
    if path in state_mgr.state["files"]:
        state_mgr.update_file(path, {"status": "synced"})
        state_mgr.save()

    # 相对 root 的 POSIX 形式：协议里所有路径都是这个形状，界面直接 root / 它 就能拼回绝对路径
    return ResolveResult(summary, backup.relative_to(local_root).as_posix() if backup else None)


def _rebase_remote(path, state_mgr, local_root, page_id, block_id, content):
    """把冲突项重新定基线到"本地胜出"：BR 刷成刚读到的远端哈希。

    `_execute_push` 的 6.6 写前校验拿当前 R 跟 BR 比，而冲突的定义就是 R ≠ BR，
    不刷基线它必然拒绝执行。刷成"刚读到的 R"之后校验仍然成立：它挡的是
    这次读取到写入之间的窗口，不是冲突本身。
    """
    state_mgr.update_file(path, {"base_remote_hash": compute_hash(content)})
    _seed_state(path, state_mgr, local_root, page_id, block_id)


def _seed_state(path, state_mgr, local_root, page_id=None, block_id=None):
    """(b) 形态（状态里压根没这个文件）在拉取/推送前补齐 entry 字段。

    `_execute_pull` 要 page_id 和 format，`_execute_push` 要 page_id，缺了就是
    KeyError。format 从本地文件探出来——`_execute_pull` 要按它还原换行与 BOM。
    """
    entry = state_mgr.get_file(path)
    updates = {}
    if page_id and not entry.get("page_id"):
        updates["page_id"] = page_id
    if block_id and not entry.get("block_id"):
        updates["block_id"] = block_id
    if not entry.get("format"):
        src = local_root / path
        if src.is_file():
            updates["format"] = detect_format(src.read_bytes())
    if updates:
        state_mgr.update_file(path, updates)


def backup_remote_content(path, remote, local_root, page_id) -> Path:
    """把远端内容落盘成一份「远端副本」，供界面做本地 ↔ 远端对比（设计 §226）。

    落点与命名跟 `_execute_conflict` 完全一致（`.sync-backup/conflicts/`），
    所以对比看到的就是「保留本地」时会被放弃的那份内容。不改状态、不做裁决。
    """
    content, _, valid = remote.read_page(page_id)
    if not valid:
        raise Exception("remote invalid-structure")
    return _write_remote_backup(path, content, local_root)


def _write_remote_backup(path, content, local_root, subdir="conflicts") -> Path:
    """覆盖/移除远端前先把远端文本备份到本地（设计 6.5 第 5 条）。

    subdir 区分来路：resolve 覆盖前是 `conflicts`，forget 归档前是 `forgotten`。
    带时间戳：同一个文件可能被解决多次，不能互相覆盖。
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    ext = PurePosixPath(path).suffix
    backup = local_root / ".sync-backup" / subdir / f"{path}.remote-{timestamp}{ext}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(content or "", encoding="utf-8")
    return backup


def _backup_local(path, local_root) -> Path | None:
    """覆盖本地前先把本地文件备份（设计 6.5）。本地不存在时返回 None。"""
    src = local_root / path
    if not src.is_file():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    backup = local_root / ".sync-backup" / "overwritten" / f"{path}.{timestamp}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, backup)
    return backup


def _add_tombstone(state_mgr, path, page_id, side):
    """写 tombstone（设计 7.1）"""
    state_mgr.state.setdefault("tombstones", {})[path] = {
        "page_id": page_id,
        "side": side,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def _file_type_from_path(path):
    """扩展名 → Notion Type 取值（未知 → text）"""
    return _FILE_TYPES.get(PurePosixPath(path).suffix.lower(), "text")


def _directory_from_path(path):
    """父目录（无父目录 → 空串）"""
    parent = PurePosixPath(path).parent
    return "" if str(parent) == "." else str(parent)
