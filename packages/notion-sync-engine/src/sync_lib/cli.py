import argparse
import os
import sys
from pathlib import Path

import yaml
from notion_client import Client

from .remote import NotionRemote
from .scanner import scan_local
from .state import StateManager, acquire_lock, cleanup_tmp_files, release_lock


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
    """同步"""
    root = Path.cwd()

    # 获取锁
    if not acquire_lock(root):
        print("错误：另一个同步进程正在运行")
        sys.exit(1)

    try:
        # 加载配置和状态
        config = yaml.safe_load((root / ".sync-config.yaml").read_text())
        state_mgr = StateManager(root)
        state_mgr.load()

        # 恢复 pending
        recovered = state_mgr.recover_pending()
        if recovered:
            print(f"发现 {len(recovered)} 个未完成操作，正在恢复…")

        # 清理临时文件
        cleanup_tmp_files(root)

        # 扫描本地
        scan_local(root, config)

        # 获取远端（简化版，实际需要实现 query_candidates）
        token = os.getenv("NOTION_TOKEN")
        client = Client(auth=token)
        NotionRemote(client, state_mgr.state["notion"]["database_id"],
                     state_mgr.state["notion"]["data_source_id"])

        # TODO: 实现完整的分类和执行逻辑
        print("同步功能待完善")

    finally:
        release_lock(root)
