"""Notion 双向同步的规划模块：三方分类与重命名检测（纯函数）。"""

from dataclasses import dataclass
from enum import Enum


class Action(Enum):
    SKIP = "skip"
    PUSH = "push"
    PULL = "pull"
    BASELINE = "baseline"
    CONFLICT = "conflict"
    BLOCKED = "blocked"
    CREATE_REMOTE = "create_remote"
    ADOPT = "adopt"
    UNMANAGED = "unmanaged"
    ARCHIVE_REMOTE = "archive_remote"
    DELETE_LOCAL = "delete_local"
    DELETE_STATE = "delete_state"
    RENAME = "rename"
    UNTRACKED = "untracked"


@dataclass
class Classification:
    action: Action
    reason: str = ""
    meta: dict = None


def classify_file(state_entry, local_hash, remote_hash, remote_exists, remote_struct_valid):
    """6.2 节：内容分类表（文件已被跟踪，两端都存在）"""
    # 检查状态
    if state_entry.get("status") == "conflict":
        return Classification(Action.BLOCKED, "status=conflict")

    # 检查结构
    if not remote_struct_valid:
        return Classification(Action.BLOCKED, "invalid-structure")

    # 内容分类
    base_local = state_entry["base_local_hash"]
    base_remote = state_entry["base_remote_hash"]

    local_changed = local_hash != base_local
    remote_changed = remote_hash != base_remote

    if not local_changed and not remote_changed:
        return Classification(Action.SKIP)
    elif local_changed and not remote_changed:
        return Classification(Action.PUSH)
    elif not local_changed and remote_changed:
        return Classification(Action.PULL)
    else:  # 都变了
        if local_hash == remote_hash:
            return Classification(Action.BASELINE)
        else:
            return Classification(Action.CONFLICT)


def classify_existence(in_state, local_exists, remote_exists, local_hash, remote_hash,
                       base_remote_hash=None, base_local_hash=None):
    """6.3 节：存在性分类表（删除与新增）"""
    if not in_state:
        if local_exists and not remote_exists:
            return Classification(Action.CREATE_REMOTE)
        elif local_exists and remote_exists:
            if local_hash == remote_hash:
                return Classification(Action.ADOPT, "same content")
            else:
                return Classification(Action.CONFLICT, "adopt conflict")
        elif not local_exists and remote_exists:
            return Classification(Action.UNMANAGED)
        else:
            return Classification(Action.SKIP)  # 都不存在

    # 状态有记录
    if local_exists == "out-of-scope":
        return Classification(Action.UNTRACKED)

    if not local_exists and remote_exists:
        if remote_hash == base_remote_hash:
            return Classification(Action.ARCHIVE_REMOTE)
        else:
            return Classification(Action.CONFLICT, "local deleted, remote changed")

    if local_exists and not remote_exists:
        if local_hash == base_local_hash:
            return Classification(Action.DELETE_LOCAL)
        else:
            return Classification(Action.CONFLICT, "remote deleted, local changed")

    if not local_exists and not remote_exists:
        return Classification(Action.DELETE_STATE)

    return Classification(Action.SKIP)


@dataclass
class Rename:
    old_path: str
    new_path: str
    page_id: str


def detect_renames(state, local_snapshot, remote_changed):
    """6.4 节：重命名检测"""
    renames = []

    # 找出本地消失的已跟踪文件
    disappeared = set(state.keys()) - set(local_snapshot.keys())

    # 找出新出现的文件
    appeared = set(local_snapshot.keys()) - set(state.keys())

    for old_path in disappeared:
        # 跳过远端已修改的
        if old_path in remote_changed:
            continue

        old_hash = state[old_path]["base_local_hash"]
        page_id = state[old_path]["page_id"]

        # 查找 hash 匹配的新文件
        matches = [p for p in appeared if local_snapshot[p]["hash"] == old_hash]

        # 只有唯一匹配才重命名
        if len(matches) == 1:
            new_path = matches[0]
            renames.append(Rename(old_path, new_path, page_id))
            appeared.remove(new_path)  # 避免重复匹配

    return renames
