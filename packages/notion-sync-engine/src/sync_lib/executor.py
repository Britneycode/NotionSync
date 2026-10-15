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

    # 2. 写前校验（6.6）：结构异常 / 远端已改 → 中止并计入 errors
    current_remote_content, _, valid = remote.read_page(entry["page_id"])
    if not valid:
        raise Exception("remote invalid-structure")
    if compute_hash(current_remote_content) != entry["base_remote_hash"]:
        raise Exception("Remote changed during push")

    # 3. 执行更新（正文超单块容量时由远端拆/并 code block）；
    #    BR 优先取写响应体（5.5），否则读回，最后本地 hash 兜底
    resp = remote.update_page_content(entry["page_id"], content)
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

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    ext = PurePosixPath(path).suffix
    backup = local_root / ".sync-backup" / "conflicts" / f"{path}.remote-{timestamp}{ext}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(content or "", encoding="utf-8")

    state_mgr.update_file(path, {"status": "conflict"})
    state_mgr.save()


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
