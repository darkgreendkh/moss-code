"""`moss runs ...` 子命令与 OTel 导出（spec-07 §4.5 / §4.8）。"""

import json

from moss.cli import main
from moss.runs.observability.otel import trace_to_otlp
from moss.runs.store import RunStore
from moss.task_state import TaskState


def _run(store, run_id, events=("run_started", "tool_executed", "run_finished")):
    state = TaskState.create(run_id=run_id, task_id="t", user_request=f"do {run_id}")
    store.start_run(state)
    for name in events:
        store.append_trace(state, {"event": name, "created_at": "2026-01-01T00:00:00+00:00"})
    state.finish_success("ok")
    store.write_task_state(state)
    store.write_report(state, {"status": state.status, "usage": {"usd": 0.25}})
    store.release_run(state.run_id)
    return state


def test_runs_list_shows_recent_runs(tmp_path, capsys):
    store = RunStore(tmp_path / ".moss" / "runs")
    _run(store, "run_a")
    _run(store, "run_b")

    code = main(["runs", "list", "--cwd", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "run_a" in out and "run_b" in out
    assert "$0.2500" in out


def test_runs_list_is_empty_without_runs(tmp_path, capsys):
    code = main(["runs", "list", "--cwd", str(tmp_path)])

    assert code == 0
    assert capsys.readouterr().out.strip() == "(no runs)"


def test_runs_show_dumps_state_report_and_lease(tmp_path, capsys):
    store = RunStore(tmp_path / ".moss" / "runs")
    _run(store, "run_a")

    code = main(["runs", "show", "run_a", "--cwd", str(tmp_path)])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["task_state"]["run_id"] == "run_a"
    assert payload["report"]["status"] == "completed"
    assert payload["trace_events"] == 3
    # 收尾时租约必须已经释放。
    assert payload["lease"] is None


def test_runs_show_reports_a_missing_run(tmp_path, capsys):
    code = main(["runs", "show", "nope", "--cwd", str(tmp_path)])

    assert code == 1
    assert "run not found" in capsys.readouterr().err


def test_runs_verify_detects_tampering(tmp_path, capsys):
    store = RunStore(tmp_path / ".moss" / "runs")
    _run(store, "run_a")

    assert main(["runs", "verify", "run_a", "--cwd", str(tmp_path)]) == 0

    path = store.trace_path("run_a")
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["event"] = "run_finished"
    lines[1] = json.dumps(tampered, sort_keys=True, ensure_ascii=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 审计工件被改过必须让脚本看得见，否则 verify 这个命令没有意义。
    assert main(["runs", "verify", "run_a", "--cwd", str(tmp_path)]) == 1
    assert "BROKEN" in capsys.readouterr().out


def test_runs_verify_without_an_id_checks_every_run(tmp_path, capsys):
    store = RunStore(tmp_path / ".moss" / "runs")
    _run(store, "run_a")
    _run(store, "run_b")

    code = main(["runs", "verify", "--cwd", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert out.count(": ok") == 2


def test_runs_prune_dry_run_reports_without_deleting(tmp_path, capsys):
    store = RunStore(tmp_path / ".moss" / "runs")
    _run(store, "run_a")

    code = main(["runs", "prune", "--keep-count", "0", "--keep-days", "0", "--dry-run", "--cwd", str(tmp_path)])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["would archive"] == ["run_a"]
    assert store.run_dir("run_a").exists()


def test_runs_prune_archives(tmp_path, capsys):
    store = RunStore(tmp_path / ".moss" / "runs")
    _run(store, "run_a")

    code = main(["runs", "prune", "--keep-count", "0", "--keep-days", "0", "--cwd", str(tmp_path)])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["archived"] == ["run_a"]
    assert not store.run_dir("run_a").exists()
    assert store.index.archive_path("run_a").exists()


def test_runs_pin_protects_from_prune(tmp_path, capsys):
    store = RunStore(tmp_path / ".moss" / "runs")
    _run(store, "run_a")

    main(["runs", "pin", "run_a", "--cwd", str(tmp_path)])
    capsys.readouterr()
    main(["runs", "prune", "--keep-count", "0", "--keep-days", "0", "--cwd", str(tmp_path)])

    assert json.loads(capsys.readouterr().out)["archived"] == []
    assert store.run_dir("run_a").exists()

    main(["runs", "pin", "run_a", "--off", "--cwd", str(tmp_path)])
    capsys.readouterr()
    main(["runs", "prune", "--keep-count", "0", "--keep-days", "0", "--cwd", str(tmp_path)])
    assert json.loads(capsys.readouterr().out)["archived"] == ["run_a"]


def test_runs_export_emits_raw_trace(tmp_path, capsys):
    store = RunStore(tmp_path / ".moss" / "runs")
    _run(store, "run_a")

    code = main(["runs", "export", "run_a", "--cwd", str(tmp_path)])

    events = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [event["event"] for event in events] == ["run_started", "tool_executed", "run_finished"]


def test_runs_export_otel_emits_otlp_json(tmp_path, capsys):
    store = RunStore(tmp_path / ".moss" / "runs")
    _run(store, "run_a")

    code = main(["runs", "export", "run_a", "--otel", "--cwd", str(tmp_path)])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    # 一个根 span（run 自己）+ 每条事件一个。
    assert len(spans) == 4
    assert spans[0]["name"] == "run_a"
    assert spans[0]["parentSpanId"] == ""
    assert all(span["traceId"] == spans[0]["traceId"] for span in spans)
    assert all(span["parentSpanId"] == spans[0]["spanId"] for span in spans[1:])


def test_runs_export_reports_a_missing_trace(tmp_path, capsys):
    code = main(["runs", "export", "nope", "--cwd", str(tmp_path)])

    assert code == 1
    assert "no trace" in capsys.readouterr().err


def test_otel_marks_failures_and_keeps_durations():
    events = [
        {"event": "tool_executed", "created_at": "2026-01-01T00:00:00+00:00", "duration_ms": 250, "tool_status": "error", "event_id": "r:1"},
        {"event": "run_finished", "created_at": "2026-01-01T00:00:01+00:00", "status": "completed", "event_id": "r:2"},
    ]

    spans = trace_to_otlp("r", events)["resourceSpans"][0]["scopeSpans"][0]["spans"]

    tool_span = spans[1]
    assert tool_span["status"]["code"] == 2
    assert int(tool_span["endTimeUnixNano"]) - int(tool_span["startTimeUnixNano"]) == 250_000_000
    assert spans[2]["status"]["code"] == 1


def test_otel_span_ids_are_stable_and_well_formed():
    events = [{"event": "run_started", "created_at": "2026-01-01T00:00:00+00:00", "event_id": "r:1"}]

    first = trace_to_otlp("run_x", events)
    second = trace_to_otlp("run_x", events)

    assert first == second
    span = first["resourceSpans"][0]["scopeSpans"][0]["spans"][1]
    assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16
    int(span["traceId"], 16)
