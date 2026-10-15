"""历史页：列表、回放、以及「刷新后还选着原来那条」。

数据走真的文件（`services.history` 写、历史页读），只把记录目录换成 tmp_path。
"""

import json

import pytest
from PySide6.QtCore import Qt

from notion_sync_desktop.pages.history_page import (
    HistoryPage,
    RunListModel,
    result_text,
    summary_text,
)
from notion_sync_desktop.services import history as history_service


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """把记录目录指到临时目录；`list_runs` 内部也调 `runs_dir()`，一处就够。"""
    directory = tmp_path / "runs"
    directory.mkdir()
    monkeypatch.setattr(history_service, "runs_dir", lambda: directory)
    return directory


def write_run(directory, stamp: str, command: str, events: list[dict]) -> None:
    (directory / f"{stamp}-{command}.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8"
    )


def run_events(command="sync", exit_code=0, rows=None, results=None) -> list[dict]:
    events = [
        {"type": "run.started", "data": {"command": command, "root": "D:/Demo/003"}},
        {
            "type": "plan",
            "data": {"fingerprint": "fp-1", "counts": {}, "rows": rows or []},
        },
    ]
    events += [
        {"type": "file.finished", "data": d} for d in (results or [])
    ]
    events.append(
        {
            "type": "run.finished",
            "data": {"exit_code": exit_code, "duration_ms": 900, "summary": {"pushed": 1}},
        }
    )
    events.append({"type": "app.exit", "data": {"exit_code": exit_code, "crashed": False}})
    return events


def make_row(path="a.md", action="PUSH") -> dict:
    return {"path": path, "action": action, "reason": "L≠BL", "local": "changed", "remote": "same"}


@pytest.fixture
def page(runs, qtbot):
    widget = HistoryPage()
    qtbot.addWidget(widget)
    yield widget
    widget.deleteLater()


# ---------------------------------------------------------------- 列表


def test_lists_runs_newest_first(runs, page):
    write_run(runs, "20261014T120000", "push", run_events("push"))
    write_run(runs, "20261015T181200", "sync", run_events("sync"))

    page.refresh()

    assert page.model.run_count() == 2
    assert page.model.run(0).command == "sync"
    assert page.model.data(page.model.index(0, 0)) == "2026-10-15 18:12:00"
    assert page.model.data(page.model.index(0, 1)) == "sync"
    assert "记录目录" in page.folderLabel.text()


def test_empty_directory_shows_nothing(page):
    assert page.model.run_count() == 0
    assert page.plan_model.row_count() == 0


# ---------------------------------------------------------------- 回放


def test_clicking_a_run_replays_its_plan_with_its_results(runs, page):
    rows = [make_row("a.md"), make_row("b.md", "PULL")]
    results = [
        {"path": "a.md", "action": "PUSH", "result": "ok", "status": "", "error": None},
        {"path": "b.md", "action": "PULL", "result": "failed", "status": "", "error": "boom"},
    ]
    write_run(runs, "20261015T181200", "sync", run_events(rows=rows, results=results))
    page.refresh()

    page.table.selectRow(0)
    page.table.clicked.emit(page.model.index(0, 0))

    assert page.plan_model.row_count() == 2
    assert page.plan_model.row(0)["result"] == "成功"
    assert page.plan_model.row(1)["result"] == "失败"
    assert page.plan_model.row(1)["error"] == "boom"
    assert "2026-10-15 18:12:00" in page.titleLabel.text()


def test_rows_without_a_result_stay_pending(runs, page):
    """预览那次运行（status）只有计划、没有 file.finished。"""
    write_run(runs, "20261015T181200", "status", run_events("status", rows=[make_row()]))
    page.refresh()

    page.replay(page.model.run(0))

    assert page.plan_model.row(0)["result"] == "待执行"


def test_default_filter_hides_skip_rows(runs, page):
    rows = [make_row("a.md"), make_row("b.md", "SKIP"), make_row("c.md", "CONFLICT")]
    write_run(runs, "20261015T181200", "sync", run_events(rows=rows))
    page.refresh()
    page.replay(page.model.run(0))

    assert page.plan_model.row_count() == 3
    # 默认停在「待处理」：只有 PUSH 算，SKIP 和 CONFLICT 都在别的分段里
    assert page.planTable.model().rowCount() == 1

    page.filter.setCurrentItem("全部")
    assert page.planTable.model().rowCount() == 3


def test_refresh_keeps_the_selected_run(runs, page):
    write_run(runs, "20261014T120000", "push", run_events("push", rows=[make_row()]))
    write_run(runs, "20261015T181200", "sync", run_events("sync", rows=[make_row()]))
    page.refresh()
    page.table.selectRow(1)  # push 那条
    page.replay(page.model.run(1))
    assert "push" in page.titleLabel.text()

    page.refresh()

    assert page.table.currentIndex().row() == 1
    assert "push" in page.titleLabel.text()
    assert page.plan_model.row_count() == 1


# ---------------------------------------------------------------- 文案


def test_result_text_covers_every_ending(runs, page):
    write_run(runs, "20261015T181200", "sync", run_events(exit_code=2))
    write_run(runs, "20261015T181300", "sync", run_events())
    broken = runs / "20261015T181400-sync.jsonl"
    broken.write_text(
        json.dumps({"type": "run.started", "data": {"command": "sync"}}) + "\n", encoding="utf-8"
    )

    page.refresh()

    assert result_text(page.model.run(2)) == "完成，有冲突需要处理"
    assert result_text(page.model.run(1)) == "完成"
    assert result_text(page.model.run(0)) == "未完成"


def test_result_text_marks_a_crash(runs, page):
    broken = runs / "20261015T181200-sync.jsonl"
    broken.write_text(
        json.dumps({"type": "app.exit", "data": {"exit_code": -1, "crashed": True}}) + "\n",
        encoding="utf-8",
    )

    page.refresh()

    assert result_text(page.model.run(0)) == "引擎异常退出（-1）"


def test_summary_text_lists_only_non_zero_fields():
    assert summary_text({"pushed": 3, "skipped": 0, "conflicts": 2}) == "push=3，conflict=2"
    assert summary_text({}) == "没有动作"


def test_run_list_model_is_read_only():
    model = RunListModel()
    assert model.columnCount() == 5
    assert model.headerData(1, Qt.Horizontal) == "命令"
    assert model.data(model.index(0, 0)) is None  # 没有行也就不越界
