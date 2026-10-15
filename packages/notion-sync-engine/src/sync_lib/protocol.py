"""engine ↔ UI 的 JSONL 事件协议（设计 v2 §3）。

本模块不依赖任何 UI 框架，是协议的唯一事实来源：版本号、退出码、
计划行的序列化，以及计划指纹。
"""

import hashlib
import json
from typing import Any

PROTOCOL_VERSION = 1


class EXIT:
    """退出码（设计 §3.4）。"""

    OK = 0  # 成功
    FAILED = 1  # 运行完成，但有文件失败
    NEEDS_ATTENTION = 2  # 有冲突或被阻塞的文件，需要用户处理
    FATAL = 3  # 致命错误（配置、token、网络中断等）
    LOCK_BUSY = 75  # 锁被占用
    PLAN_CHANGED = 76  # 计划已变化（使用 --expect-plan 时）
    CANCELLED = 130  # 已取消


def side_label(exists: bool, current: str | None, base: str | None) -> str:
    """把一侧与基线对比的结果转成 UI 可显示的标签。

    `base is None` 表示这一侧从未建立过基线（新文件），与"内容相同"要区分开。
    """
    if not exists:
        return "absent"
    if base is None:
        return "new"
    return "same" if current == base else "changed"


def plan_row(
    path: str,
    classification,
    local_label: str = "",
    remote_label: str = "",
) -> dict[str, Any]:
    """把一条 Classification 转成协议计划行（设计 §3.3）。"""
    return {
        "path": path,
        "action": classification.action.name,
        "reason": classification.reason or "",
        "local": local_label,
        "remote": remote_label,
    }


def plan_fingerprint(plan: dict, local_hashes=None, remote_hashes=None) -> str:
    """计划指纹（设计 §3.5）：非 SKIP 行按路径排序后，
    取 (path, action, L, R) 序列的 sha256。

    L / R 由调用方从本地快照和远端索引里取（plan 本身不含内容哈希）。
    """
    local_hashes = local_hashes or {}
    remote_hashes = remote_hashes or {}
    rows = sorted(
        (path, c.action.name, local_hashes.get(path), remote_hashes.get(path))
        for path, c in plan.items()
        if c.action.name != "SKIP"
    )
    blob = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
