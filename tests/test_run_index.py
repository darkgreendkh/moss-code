"""run 索引与保留策略（spec-07 §4.8）。

守两件事：一千个 run 下启动仍然快；pinned / 在跑 / 被评测引用的 run 永不被清理。
"""

import gzip
import json
import time
from datetime import datetime, timedelta, timezone

from moss.runs.index import (
    ARCHIVE_SUFFIX,
    DEFAULT_RETENTION_COUNT,
    DEFAULT_RETENTION_DAYS,
    RunIndex,
    archive_run_dir,
    expired_run_ids,
    read_archive,
    referenced_run_ids,
    retention_limits,
)
from moss.runs.store import RunStore
from moss.task_state import STATUS_RUNNING, TaskState


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _finished_run(store, run_id, started_at=None):
    state = TaskState.create(run_id=run_id, task_id="t_" + run_id, user_request=f"do {run_id}")
    if started_at:
        state.started_at = started_at
    store.start_run(state)
    state.finish_success("ok")
    store.write_task_state(state)
    store.write_report(state, {"status": state.status, "usage": {"usd": 0.5}})
    store.release_run(state.run_id)
    return state


def test_index_records_start_and_finish(tmp_path):
    store = RunStore(tmp_path / "runs")
    _finished_run(store, "run_1")

    entries = store.index.entries()

    assert [entry["run_id"] for entry in entries] == ["run_1"]
    assert entries[0]["status"] == "completed"
    assert entries[0]["stop_reason"] == "final_answer_returned"
    assert entries[0]["cost_usd"] == 0.5
    assert entries[0]["task_summary"] == "do run_1"


def test_index_folds_updates_last_write_wins(tmp_path):
    index = RunIndex(tmp_path)
    index.record("r1", status=STATUS_RUNNING, task_summary="hello")
    index.record("r1", status="completed")

    entries = index.entries()

    assert len(entries) == 1
    # 只更新传进来的字段，其余沿用之前那条。
    assert entries[0]["status"] == "completed"
    assert entries[0]["task_summary"] == "hello"


def test_index_compacts_itself(tmp_path):
    index = RunIndex(tmp_path)
    for _ in range(30):
        index.record("r1", status=STATUS_RUNNING)

    lines = index.path.read_text(encoding="utf-8").splitlines()

    assert len(lines) < 30
    assert index.entries()[0]["run_id"] == "r1"


def test_startup_with_a_thousand_runs_is_fast(tmp_path):
    """验收门槛（spec-07 §7）：1000 个 run 下启动 <100ms。"""
    root = tmp_path / "runs"
    store = RunStore(root)
    for index in range(1000):
        directory = root / f"run_{index:04d}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "task_state.json").write_text(
            json.dumps({"run_id": f"run_{index:04d}", "status": "completed", "user_request": "x"}),
            encoding="utf-8",
        )
    store.ensure_index()

    reopened = RunStore(root)
    started = time.monotonic()
    running = reopened.find_running_runs()
    elapsed_ms = (time.monotonic() - started) * 1000

    assert running == []
    # 索引里没有 running，所以一个 run 目录都不该被打开。留一倍余量抗 CI 抖动。
    assert elapsed_ms < 200, f"startup scan took {elapsed_ms:.0f}ms"


def test_index_is_rebuilt_when_missing(tmp_path):
    root = tmp_path / "runs"
    store = RunStore(root)
    _finished_run(store, "run_1")
    store.index.path.unlink()

    rebuilt = RunStore(root)
    rebuilt.ensure_index()

    assert [entry["run_id"] for entry in rebuilt.index.entries()] == ["run_1"]


def test_running_runs_are_found_through_the_index(tmp_path):
    store = RunStore(tmp_path / "runs")
    running = TaskState.create(run_id="run_live", task_id="t", user_request="x")
    store.start_run(running)
    _finished_run(store, "run_done")

    assert [state.run_id for state in store.find_running_runs()] == ["run_live"]


def test_expired_runs_are_archived_not_deleted(tmp_path):
    store = RunStore(tmp_path / "runs")
    old = _finished_run(store, "run_old", started_at=_ago(90))
    _finished_run(store, "run_new")

    archived = store.prune(keep_count=1, keep_days=30)

    assert archived == ["run_old"]
    assert not store.run_dir(old.run_id).exists()
    archive = store.index.archive_path("run_old")
    assert archive.exists()
    files = read_archive(archive)
    # 归档不是黑盒：解开还是逐文件的 jsonl，report/trace 都能翻出来。
    assert "task_state.json" in files
    assert json.loads(files["task_state.json"])["run_id"] == "run_old"
    assert [entry["run_id"] for entry in store.index.entries()] == ["run_new"]


def test_pinned_runs_are_never_pruned(tmp_path):
    store = RunStore(tmp_path / "runs")
    _finished_run(store, "run_pinned", started_at=_ago(400))
    _finished_run(store, "run_other", started_at=_ago(400))
    store.pin("run_pinned")

    archived = store.prune(keep_count=0, keep_days=1)

    assert archived == ["run_other"]
    assert store.run_dir("run_pinned").exists()


def test_runs_holding_a_live_lease_are_never_pruned(tmp_path):
    store = RunStore(tmp_path / "runs")
    live = TaskState.create(run_id="run_live", task_id="t", user_request="x")
    live.started_at = _ago(400)
    store.start_run(live)

    archived = store.prune(keep_count=0, keep_days=1)

    assert archived == []
    assert store.run_dir("run_live").exists()


def test_runs_referenced_by_evaluation_artifacts_are_never_pruned(tmp_path):
    store = RunStore(tmp_path / "runs")
    _finished_run(store, "run_cited", started_at=_ago(400))
    _finished_run(store, "run_uncited", started_at=_ago(400))
    artifact = tmp_path / "artifacts" / "benchmark.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"cases": [{"run_id": "run_cited"}]}), encoding="utf-8")

    protected = referenced_run_ids([artifact])
    archived = store.prune(keep_count=0, keep_days=1, protected=protected)

    assert archived == ["run_uncited"]


def test_dry_run_changes_nothing(tmp_path):
    store = RunStore(tmp_path / "runs")
    _finished_run(store, "run_old", started_at=_ago(400))

    planned = store.prune(keep_count=0, keep_days=1, dry_run=True)

    assert planned == ["run_old"]
    assert store.run_dir("run_old").exists()


def test_recent_runs_survive_both_dimensions():
    now = datetime.now(timezone.utc)
    entries = [
        {"run_id": "a", "started_at": _ago(1)},
        {"run_id": "b", "started_at": _ago(200)},
        {"run_id": "c", "started_at": _ago(300)},
    ]

    # 只要还在最近 N 个里、或者还在 M 天内，就留着（两个维度是"或"）。
    assert expired_run_ids(entries, keep_count=2, keep_days=30, protected=(), now=now) == ["c"]
    assert expired_run_ids(entries, keep_count=1, keep_days=365, protected=(), now=now) == []
    assert expired_run_ids(entries, keep_count=0, keep_days=None, protected=(), now=now) == ["a", "b", "c"]


def test_retention_limits_read_env_and_reject_nonsense():
    assert retention_limits({}) == (DEFAULT_RETENTION_COUNT, DEFAULT_RETENTION_DAYS)
    assert retention_limits({"MOSS_RUN_RETENTION_COUNT": "50"})[0] == 50
    # 0 = 显式关掉这一维；写错的值退回默认，绝不因为一个 typo 清空历史。
    assert retention_limits({"MOSS_RUN_RETENTION_DAYS": "0"})[1] is None
    assert retention_limits({"MOSS_RUN_RETENTION_DAYS": "abc"})[1] == DEFAULT_RETENTION_DAYS


def test_archive_round_trips_nested_files(tmp_path):
    run_dir = tmp_path / "run_x"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "task_state.json").write_text('{"a": 1}', encoding="utf-8")
    (run_dir / "artifacts" / "001-out.txt").write_text("big output", encoding="utf-8")

    count = archive_run_dir(run_dir, tmp_path / ("run_x" + ARCHIVE_SUFFIX))

    assert count == 2
    files = read_archive(tmp_path / ("run_x" + ARCHIVE_SUFFIX))
    assert files["artifacts/001-out.txt"] == "big output"
    with gzip.open(tmp_path / ("run_x" + ARCHIVE_SUFFIX), "rt", encoding="utf-8") as handle:
        assert handle.read().count("\n") == 2
