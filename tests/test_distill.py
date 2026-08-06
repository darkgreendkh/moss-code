from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext, build_arg_parser
from moss.memory.service import distill_run


def _tool_event(sequence, *, name="run_shell", args=None, status="ok", exit_code=0, result=""):
    return {
        "event": "tool_executed",
        "run_id": "run-123",
        "sequence": sequence,
        "name": name,
        "args": args or {},
        "tool_status": status,
        "exit_code": exit_code,
        "result": result,
        "created_at": f"2026-08-05T10:00:0{sequence}+00:00",
    }


def test_distill_extracts_similar_failure_success_pair(tmp_path):
    events = [
        _tool_event(
            3,
            args={"command": "pytest -q", "timeout": 20},
            status="error",
            exit_code=1,
            result="ModuleNotFoundError: no module named pytest",
        ),
        {"event": "checkpoint_created", "run_id": "run-123", "sequence": 4},
        _tool_event(
            5,
            args={"command": "uv run pytest -q", "timeout": 20},
            status="ok",
            exit_code=0,
            result="12 passed",
        ),
    ]

    records = distill_run(events, workspace_root=tmp_path)

    assert len(records) == 1
    record = records[0]
    assert "pytest -q" in record.text
    assert "uv run pytest -q" in record.text
    assert "ModuleNotFoundError" in record.text
    assert record.topic == "procedural"
    assert record.trust == "model"
    assert [(source.run_id, source.event_seq) for source in record.source_refs] == [
        ("run-123", 3),
        ("run-123", 5),
    ]


def test_distill_extracts_policy_and_approval_denials(tmp_path):
    events = [
        {
            **_tool_event(
                2,
                name="write_file",
                args={"path": ".github/workflows/ci.yml", "content": "disabled"},
                status="rejected",
            ),
            "tool_error_code": "capability_denied",
        },
        {
            **_tool_event(
                4,
                name="run_shell",
                args={"command": "git push origin main"},
                status="rejected",
            ),
            "tool_error_code": "approval_denied",
        },
    ]

    records = distill_run(events, workspace_root=tmp_path)

    assert len(records) == 2
    assert all(record.trust == "model" for record in records)
    assert any(".github/workflows/ci.yml" in record.text for record in records)
    assert any("git push origin main" in record.text for record in records)


def test_distill_returns_empty_when_trace_has_no_supported_pattern(tmp_path):
    events = [
        _tool_event(1, name="read_file", args={"path": "README.md"}, result="ok"),
        {"event": "run_finished", "run_id": "run-123", "sequence": 2},
    ]

    assert distill_run(events, workspace_root=tmp_path) == []


def test_cli_exposes_reflection_mode():
    assert build_arg_parser().parse_args(["--reflect", "off"]).reflect == "off"
    assert build_arg_parser().parse_args(["--reflect", "model"]).reflect == "model"


def test_runtime_distills_and_recalls_procedural_memory(tmp_path):
    agent = Moss(
        model_client=FakeModelClient(
            [
                '<tool>{"name":"run_shell","args":{"command":"python -c \'raise SystemExit(1)\'","timeout":20}}</tool>',
                '<tool>{"name":"run_shell","args":{"command":"python -c \'raise SystemExit(0)\'","timeout":20}}</tool>',
                "<final>Recovered with the corrected command.</final>",
            ]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )

    assert agent.ask("Find the working command") == "Recovered with the corrected command."

    procedural_files = list((tmp_path / ".moss" / "memory" / "procedural").glob("mem_*.md"))
    assert len(procedural_files) == 1
    assert "SystemExit(1)" in procedural_files[0].read_text(encoding="utf-8")
    matches = agent.memory.search("SystemExit corrected command", limit=5)
    assert any(match["topic"] == "procedural" for match in matches)
