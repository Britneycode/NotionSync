"""假引擎：按 `--fake-mode` 吐出可预期的协议流，用来测 `EngineClient`。

它接收和真引擎一样的命令行（`<command> --root <dir> --json --control-stdin`），
只是不碰网络、不碰磁盘。模式通过额外参数传入：

    python fake_engine.py sync --root X --json --control-stdin --fake-mode split
"""

import json
import os
import sys
import time

RUN_ID = "r-fake"
_seq = 0


def emit(type_: str, **data) -> None:
    global _seq
    _seq += 1
    line = json.dumps(
        {
            "v": 1,
            "seq": _seq,
            "ts": "2026-10-15T00:00:00.000+00:00",
            "run": RUN_ID,
            "type": type_,
            "data": data,
        },
        ensure_ascii=False,
    )
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def flag(argv: list[str], name: str) -> str | None:
    if name in argv:
        return argv[argv.index(name) + 1]
    return None


def wait_for_cancel() -> None:
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "cancel":
            return


def main() -> int:
    argv = sys.argv[1:]
    mode = flag(argv, "--fake-mode") or "normal"

    if mode == "badversion":
        # 主版本不认识：EngineClient 应当丢掉它并只在诊断里提一句
        sys.stdout.write(json.dumps({"v": 99, "seq": 1, "type": "hello", "data": {}}) + "\n")
        sys.stdout.flush()
        return 0

    if mode == "garbage":
        sys.stdout.write("这行不是 JSON\n")
        sys.stdout.flush()
        emit("hello", engine_version="fake")
        return 0

    if mode == "stderr":
        sys.stderr.write("引擎自己的一行日志\n")
        sys.stderr.flush()
        emit("hello", engine_version="fake")
        return 0

    if mode == "split":
        # 一行 JSON 分两次写出来，中间还 flush 一次：模拟半行到达
        line = json.dumps({"v": 1, "seq": 1, "run": RUN_ID, "type": "hello", "data": {"a": 1}})
        cut = len(line) // 2
        sys.stdout.write(line[:cut])
        sys.stdout.flush()
        time.sleep(0.2)
        sys.stdout.write(line[cut:] + "\n")
        sys.stdout.flush()
        return 0

    if mode == "crash":
        emit("hello", engine_version="fake")
        os._exit(3)  # 不做任何收尾，模拟引擎崩溃

    if mode == "tail":
        # 最后一行写一半就死了：没有换行符，客户端不该把它丢掉
        emit("hello", engine_version="fake")
        sys.stdout.write('{"v":1,"seq":2,"type":"run.finis')
        sys.stdout.flush()
        return 3

    if mode == "cancel":
        emit("hello", engine_version="fake")
        emit("run.started", command="sync", root=flag(argv, "--root") or "", options={})
        emit("phase", name="execute")
        emit("file.started", path="a.md", action="PUSH", index=1, total=2)
        wait_for_cancel()
        emit("run.cancelled", done=1, remaining=1)
        return 130

    if mode == "ignore-cancel":
        # 卡住不理会取消请求：用来测 15 秒后升级强杀
        emit("hello", engine_version="fake")
        emit("phase", name="execute")
        time.sleep(60)
        return 0

    # normal
    emit("hello", engine_version="fake")
    emit("run.started", command="sync", root=flag(argv, "--root") or "", options={})
    emit("phase", name="scan")
    emit("plan", fingerprint="fp", rows=[], counts={})
    emit("file.started", path="a.md", action="PUSH", index=1, total=1)
    emit("file.finished", path="a.md", action="PUSH", result="ok", status="", error=None)
    emit("progress", done=1, total=1)
    emit("run.finished", exit_code=0, duration_ms=1, executed=True, summary={"pushed": 1})
    return 0


if __name__ == "__main__":
    sys.exit(main())
