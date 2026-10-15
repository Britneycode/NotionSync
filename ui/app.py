#!/usr/bin/env python3
"""Notion 同步 · 桌面界面

界面进程只负责显示；真正的同步通过子进程调用 engine/sync.py 完成，
两者共用同一个 .venv，互不干扰。同步逻辑零重复实现。
"""
import json
import os
import subprocess
import threading
import traceback
from pathlib import Path

import webview

HERE = Path(__file__).resolve().parent          # D:\Demo\notion\ui
BASE = HERE.parent                              # D:\Demo\notion
ENGINE = BASE / "engine" / "sync.py"
PYTHON = BASE / ".venv" / "Scripts" / "python.exe"
INDEX = HERE / "index.html"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "last-error.log"

COMMANDS = {"init", "status", "push", "pull", "sync"}

# token 只从环境变量读，界面里临时输入的仅本次运行有效 —— 机密不落盘
# （沿用 2026-10-14 决策：token 仅从 NOTION_TOKEN 读取）
SESSION_TOKEN = ""


def read_token():
    env = (os.getenv("NOTION_TOKEN") or "").strip()
    if env:
        return env, "环境变量"
    if SESSION_TOKEN:
        return SESSION_TOKEN, "本次输入"
    return "", ""


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_args(folder, command, dry_run=False, parent_page_id=""):
    """把界面输入翻译成 sync.py 的命令行参数。

    成功返回 (目标目录, 参数表, None)；失败返回 (None, None, 原因)。
    """
    if command not in COMMANDS:
        return None, None, f"未知命令 {command}"
    target = Path(folder or "")
    if not target.is_dir():
        return None, None, "文件夹不存在"

    args = [str(PYTHON), str(ENGINE), command]
    if command == "init":
        pid = (parent_page_id or "").strip()
        if not pid:
            return None, None, "请填写 Notion 父页面 ID"
        args += ["--parent-page-id", pid]
    elif dry_run:
        args.append("--dry-run")
    return target, args, None


class Api:
    def __init__(self):
        self.window = None
        self.proc = None
        self.running = False
        self.lines = []
        self.exit_code = None
        self._lock = threading.Lock()

    # ---------- 前端可调用的接口 ----------

    def bootstrap(self):
        token, source = read_token()
        return {
            "engine": ENGINE.exists(),
            "python": PYTHON.exists(),
            "has_token": bool(token),
            "token_source": source,
            "last_folder": load_state().get("last_folder", ""),
        }

    def pick_folder(self):
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else None

    def probe(self, folder):
        p = Path(folder or "")
        return {
            "exists": p.is_dir(),
            "initialized": (p / ".sync-config.yaml").exists(),
        }

    def use_token(self, token):
        global SESSION_TOKEN
        token = (token or "").strip()
        if not token:
            return {"ok": False, "error": "token 不能为空"}
        SESSION_TOKEN = token
        return {"ok": True}

    def start(self, folder, command, dry_run=False, parent_page_id=""):
        if self.running:
            return {"ok": False, "error": "已有任务在运行，请先停止"}

        target, args, error = build_args(folder, command, dry_run, parent_page_id)
        if error:
            return {"ok": False, "error": error}

        token, _ = read_token()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        if token:
            env["NOTION_TOKEN"] = token

        with self._lock:
            self.lines = []
            self.exit_code = None
        self.running = True

        if command != "status":
            save_state({"last_folder": str(target)})

        def worker():
            try:
                self.proc = subprocess.Popen(
                    args, cwd=str(target), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
                for line in self.proc.stdout:
                    with self._lock:
                        self.lines.append(line)
                code = self.proc.wait()
            except Exception as exc:  # 启动失败也要让界面看到
                with self._lock:
                    self.lines.append(f"[启动失败] {exc}\n")
                code = -1
            with self._lock:
                self.exit_code = code
            self.proc = None
            self.running = False

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "command": command}

    def poll(self):
        with self._lock:
            chunk = "".join(self.lines)
            self.lines.clear()
            code = self.exit_code
            self.exit_code = None
        return {"text": chunk, "running": self.running, "exit_code": code}

    def stop(self):
        if self.proc is None:
            return {"ok": False}
        self.proc.kill()
        return {"ok": True}


def main():
    api = Api()
    window = webview.create_window(
        "Notion 同步",
        str(INDEX),
        js_api=api,
        width=920,
        height=680,
        min_size=(720, 520),
        background_color="#0e1116",
    )
    api.window = window
    webview.start()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG_FILE.write_text(traceback.format_exc(), encoding="utf-8")
        raise
