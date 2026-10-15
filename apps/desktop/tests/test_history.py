"""运行历史：写下来的文件能被原样读回。

这一层不碰 Qt，纯文件读写——但它是历史页唯一的真相来源，所以边界都钉住：
半行、缺事件、写不进去、连跑两次。
"""

import json

from notion_sync_desktop.services import history


def run_started(command="sync", root="D:/Demo/003") -> dict:
    return {"v": 1, "run": "r-1", "type": "run.started", "data": {"command": command, "root": root}}


def plan_event(path="a.md", action="PUSH") -> dict:
    return {
        "v": 1,
        "run": "r-1",
        "type": "plan",
        "data": {
            "fingerprint": "fp-1",
            "counts": {action: 1},
            "rows": [{"path": path, "action": action, "reason": "L≠BL", "local": "changed", "remote": "same"}],
        },
    }


def file_finished(path="a.md", result="ok", error=None) -> dict:
    return {
        "v": 1,
        "run": "r-1",
        "type": "file.finished",
        "data": {"path": path, "action": "PUSH", "result": result, "status": "", "error": error},
    }


def run_finished(exit_code=0, summary=None) -> dict:
    return {
        "v": 1,
        "run": "r-1",
        "type": "run.finished",
        "data": {
            "exit_code": exit_code,
            "duration_ms": 1234,
            "executed": True,
            "summary": summary if summary is not None else {"pushed": 1, "skipped": 3},
        },
    }


# ---------------------------------------------------------------- 写


def test_writer_round_trip(tmp_path):
    writer = history.HistoryWriter(tmp_path)
    for ev in (run_started(), plan_event(), file_finished(), run_finished()):
        writer.write(ev)
    writer.finish(0, False)

    assert writer.path is not None
    assert writer.path.parent == tmp_path
    assert writer.path.name.endswith("-sync.jsonl")

    record = history.load_run(writer.path)
    assert record.command == "sync"
    assert record.root == "D:/Demo/003"
    assert record.exit_code == 0
    assert record.crashed is False
    assert record.duration_ms == 1234
    assert record.summary == {"pushed": 1, "skipped": 3}
    assert record.fingerprint == "fp-1"
    assert [r["path"] for r in record.rows] == ["a.md"]
    assert [r["result"] for r in record.results] == ["ok"]


def test_writer_starts_a_new_file_for_the_next_run(tmp_path):
    writer = history.HistoryWriter(tmp_path)
    writer.write(run_started(command="push"))
    first = writer.path
    writer.finish(0, False)

    writer.write(run_started(command="pull"))
    second = writer.path
    writer.finish(0, False)

    assert first != second
    assert history.load_run(first).command == "push"
    assert history.load_run(second).command == "pull"
    # 第二次运行的事件只落在第二个文件里
    assert history.load_run(first).results == []


def test_writer_abandons_the_old_file_if_finish_never_came(tmp_path):
    """引擎被强杀时 `finished` 也可能没走完；下一次运行不能被写进旧文件。"""
    writer = history.HistoryWriter(tmp_path)
    writer.write(run_started(command="push"))
    first = writer.path

    writer.write(run_started(command="pull"))

    assert writer.path != first
    assert history.load_run(writer.path).command == "pull"
    assert history.load_run(writer.path).results == []


def test_app_exit_overrides_the_protocol_exit_code(tmp_path):
    """引擎崩了就没有 run.finished，进程退出码才是真的。"""
    writer = history.HistoryWriter(tmp_path)
    writer.write(run_started())
    writer.write(run_finished(exit_code=0))
    writer.finish(-1, True)

    record = history.load_run(writer.path)
    assert record.exit_code == -1
    assert record.crashed is True


def test_finish_without_a_started_run_writes_nothing(tmp_path):
    writer = history.HistoryWriter(tmp_path)
    writer.finish(1, True)

    assert writer.path is None
    assert list(tmp_path.glob("*.jsonl")) == []


def test_unwritable_directory_is_not_fatal(tmp_path):
    """记录目录建不起来（比如被一个同名文件占了）：不抛，只是没历史。"""
    blocked = tmp_path / "runs"
    blocked.write_text("我不是目录", encoding="utf-8")

    writer = history.HistoryWriter(blocked)
    writer.write(run_started())
    writer.finish(0, False)

    assert writer.path is None


# ---------------------------------------------------------------- 读


def write_run(directory, name: str, events: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8"
    )


def test_list_runs_is_newest_first(tmp_path):
    write_run(tmp_path, "20261013T090000-sync", [run_started()])
    write_run(tmp_path, "20261015T181200-pull", [run_started(command="pull")])
    write_run(tmp_path, "20261014T120000-push", [run_started(command="push")])

    records = history.list_runs(tmp_path)

    assert [r.command for r in records] == ["pull", "push", "sync"]
    assert records[0].when == "2026-10-15 18:12:00"


def test_list_runs_honours_the_limit(tmp_path):
    for stamp in ("20261013T090000", "20261014T090000", "20261015T090000"):
        write_run(tmp_path, f"{stamp}-sync", [run_started()])

    assert len(history.list_runs(tmp_path, limit=2)) == 2
    assert history.list_runs(tmp_path, limit=2)[0].when == "2026-10-15 09:00:00"


def test_list_runs_without_a_directory_is_empty(tmp_path):
    assert history.list_runs(tmp_path / "不存在") == []


def test_broken_lines_do_not_lose_the_rest(tmp_path):
    """半行/坏行来自强杀，剩下的部分照样能回放。"""
    directory = tmp_path
    directory.mkdir(exist_ok=True)
    body = "\n".join(
        [
            json.dumps(run_started()),
            '{"v": 1, "type": "plan", "data": {"fingerprint"',  # 写到一半被杀
            "这不是 JSON",
            json.dumps(file_finished()),
            json.dumps(run_finished(exit_code=1)),
            "",
        ]
    )
    path = directory / "20261015T181200-sync.jsonl"
    path.write_text(body, encoding="utf-8")

    record = history.load_run(path)

    assert record.command == "sync"
    assert record.exit_code == 1
    assert [r["path"] for r in record.results] == ["a.md"]


def test_record_without_events_keeps_the_name(tmp_path):
    path = tmp_path / "20261015T181200-sync.jsonl"
    path.write_text("", encoding="utf-8")

    record = history.load_run(path)

    assert record.command == "sync"
    assert record.exit_code is None  # 没走到收尾
    assert record.rows == []


def test_format_stamp_falls_back_to_the_raw_value():
    assert history.format_stamp("20261015T181200") == "2026-10-15 18:12:00"
    assert history.format_stamp("乱写的") == "乱写的"
