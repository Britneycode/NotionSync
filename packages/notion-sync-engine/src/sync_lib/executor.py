"""执行同步计划：PUSH/PULL 处理器与乐观并发写前校验（设计文档 6.6 节）。"""

from dataclasses import dataclass

from .planner import Action


@dataclass
class Summary:
    pushed: int = 0
    pulled: int = 0
    created: int = 0
    archived: int = 0
    deleted: int = 0
    renamed: int = 0
    conflicts: int = 0
    skipped: int = 0
    errors: list = None


def execute_plan(plan, state_mgr, remote, local_root, config):
    """执行同步计划"""
    summary = Summary(errors=[])

    for path, classification in plan.items():
        action = classification.action

        try:
            if action == Action.PUSH:
                _execute_push(path, state_mgr, remote, local_root)
                summary.pushed += 1
            elif action == Action.PULL:
                _execute_pull(path, state_mgr, remote, local_root)
                summary.pulled += 1
            # ... 其他 action
        except Exception as e:
            summary.errors.append({"path": path, "error": str(e)})

    return summary


def _execute_push(path, state_mgr, remote, local_root):
    """执行 PUSH"""
    # 1. 写意图
    content = (local_root / path).read_text(encoding="utf-8-sig")
    from .canonical import compute_hash
    content_hash = compute_hash(content)
    state_mgr.set_pending(path, "push", content_hash)

    # 2. 写前校验（6.6 节）
    entry = state_mgr.get_file(path)
    current_remote_content, _, _ = remote.read_page(entry["page_id"])
    from .canonical import compute_hash as ch
    if ch(current_remote_content) != entry["base_remote_hash"]:
        raise Exception("Remote changed during push")

    # 3. 执行更新
    remote.update_block(entry["block_id"], content)

    # 4. 更新状态
    state_mgr.update_file(path, {
        "base_local_hash": content_hash,
        "base_remote_hash": content_hash,
    })
    state_mgr.clear_pending(path)


def _execute_pull(path, state_mgr, remote, local_root):
    """执行 PULL"""
    entry = state_mgr.get_file(path)

    # 1. 写意图
    remote_content, block_id, valid = remote.read_page(entry["page_id"])
    from .canonical import compute_hash, restore_format
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
