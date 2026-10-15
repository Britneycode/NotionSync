"""Notion 双向同步的状态管理：原子读写 .sync-state.json、pending 意图日志、
崩溃恢复与进程锁（.sync.lock）。"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psutil


class StateManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.state_file = self.root / ".sync-state.json"
        self.tmp_file = self.root / ".sync-state.json.sync-tmp"
        self.lock_file = self.root / ".sync.lock"
        self.state = None

    def load(self):
        """加载状态文件"""
        if not self.state_file.exists():
            self.state = {
                "schema_version": 2,
                "files": {},
                "tombstones": {},
                "skipped": [],
            }
        else:
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))

    def save(self):
        """原子写入状态文件"""
        # 写临时文件并 fsync 确保落盘（同一句柄，兼容 Windows）
        with open(self.tmp_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.state, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())

        # 原子替换
        os.replace(self.tmp_file, self.state_file)

    def get_file(self, path: str) -> dict:
        """获取文件状态"""
        return self.state["files"].get(path, {})

    def update_file(self, path: str, updates: dict):
        """更新文件状态"""
        if path not in self.state["files"]:
            self.state["files"][path] = {}
        self.state["files"][path].update(updates)

    def set_pending(self, path: str, op: str, content_hash: str):
        """设置 pending 意图"""
        self.state["files"][path]["pending"] = {
            "op": op,
            "content_hash": content_hash,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save()

    def clear_pending(self, path: str):
        """清除 pending"""
        if path in self.state["files"]:
            self.state["files"][path]["pending"] = None
            self.save()

    def recover_pending(self) -> list:
        """启动时恢复 pending 记录"""
        recovered = []
        for path, entry in self.state["files"].items():
            pending = entry.get("pending")
            if not pending:
                continue

            op = pending["op"]
            if op == "create":
                if "page_id" not in entry:
                    recovered.append({"path": path, "action": "adopt_or_skip"})
            elif op in ("push", "pull", "resolve"):
                # 清掉 pending，交给正常分类流程
                self.clear_pending(path)
                recovered.append({"path": path, "action": "reclassify"})
            elif op in ("archive", "delete-local", "rename"):
                recovered.append({"path": path, "action": "check_target_state"})

        return recovered


def acquire_lock(root: Path) -> bool:
    """获取进程锁"""
    lock_file = Path(root) / ".sync.lock"

    if lock_file.exists():
        try:
            data = json.loads(lock_file.read_text())
            pid = data["pid"]
            # 检查进程是否存活
            if psutil.pid_exists(pid):
                return False
            else:
                # 锁过期，删除
                lock_file.unlink()
        except Exception:
            # 锁文件损坏，删除
            lock_file.unlink()

    # 创建锁
    lock_file.write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }))
    return True


def release_lock(root: Path):
    """释放进程锁"""
    lock_file = Path(root) / ".sync.lock"
    if lock_file.exists():
        lock_file.unlink()


def cleanup_tmp_files(root: Path):
    """清理残留的 .sync-tmp 文件"""
    for tmp_file in Path(root).glob("*.sync-tmp"):
        tmp_file.unlink()
