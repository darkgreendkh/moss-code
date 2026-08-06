"""结构化上下文压缩（spec-06 §4.1）。

压缩最贵的一种错误是把"部分成功"摘要成"成功"——模型据此认为事情做完了，
而实际上文件只改了一半。所以这里守的不是"摘要短不短"，而是：
可逆、幂等、闭合、因果单元不可拆、partial_success 不被洗白。
"""

import json

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.compaction import (
    COMPACTION_SCHEMA_VERSION,
    KEEP_RECENT_STEPS,
    compact,
    compactable_steps,
    execution_steps,
    render_compaction,
    step_is_complete,
)


def build_agent(tmp_path, outputs=(), **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


def _history(steps=8):
    history = [{"role": "user", "content": "make the parser accept unicode", "created_at": "2026-04-07T09:00:00+00:00"}]
    for index in range(steps):
        history.append(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": f"moss/module_{index}.py", "start": 1, "end": 40},
                "content": f"# moss/module_{index}.py\n   1: def handler_{index}():\n   2:     pass",
                "created_at": f"2026-04-07T09:{index + 1:02d}:00+00:00",
            }
        )
    return history


def _events(history):
    events = []
    sequence = 0
    for item in history:
        if item.get("role") != "tool":
            continue
        sequence += 1
        events.append(
            {
                "event": "tool_executed",
                "sequence": sequence,
                "name": item["name"],
                "args": item["args"],
                "result": item["content"],
                "tool_status": "ok",
                "tool_error_code": "",
                "affected_paths": [],
            }
        )
    return events


def test_compaction_is_idempotent_for_the_same_input():
    history = _history()
    events = _events(history)

    first, first_remaining = compact(history, events, created_at="2026-04-07T10:00:00+00:00")
    second, second_remaining = compact(history, events, created_at="2026-04-07T11:00:00+00:00")

    assert first.fingerprint() == second.fingerprint()
    assert first.id == second.id
    assert first_remaining == second_remaining
    assert first.schema_version == COMPACTION_SCHEMA_VERSION


def test_covered_range_is_closed_over_the_history():
    history = _history()

    artifact, remaining = compact(history, _events(history))

    # 覆盖 + 保留 = 全集。没有条目会被"顺手丢掉"。
    assert artifact.covered_history_count + len(remaining) == len(history)
    assert artifact.kept_history_count == len(remaining)
    assert remaining == history[artifact.covered_history_count:]


def test_recent_steps_are_kept_verbatim():
    history = _history()

    _, remaining = compact(history, _events(history))

    assert len(remaining) == KEEP_RECENT_STEPS
    assert remaining[-1]["content"].endswith("pass")


def test_causal_units_are_never_split():
    history = [
        {"role": "user", "content": "start", "created_at": "2026-04-07T09:00:00+00:00"},
        {"role": "assistant", "name": "read_file", "args": {"path": "a.py"}, "call_id": "c1",
         "native_tool_call": True, "content": "", "created_at": "2026-04-07T09:01:00+00:00"},
        {"role": "tool", "name": "read_file", "args": {"path": "a.py"}, "call_id": "c1",
         "content": "# a.py", "created_at": "2026-04-07T09:02:00+00:00"},
        {"role": "system", "content": "Runtime notice: something", "created_at": "2026-04-07T09:03:00+00:00"},
        {"role": "assistant", "name": "read_file", "args": {"path": "b.py"}, "call_id": "c2",
         "native_tool_call": True, "content": "", "created_at": "2026-04-07T09:04:00+00:00"},
    ]

    steps = execution_steps(history)

    assert [len(step) for step in steps] == [1, 3, 1]
    assert step_is_complete(steps[1]) is True
    # 有调用无结果的那一组是残缺的，绝不能被压进摘要里。
    assert step_is_complete(steps[2]) is False
    compacted, kept = compactable_steps(steps, keep_recent=0)
    assert compacted == steps[:2]
    assert kept == steps[2:]


def test_partial_success_is_never_summarized_as_completed():
    history = [
        {"role": "user", "content": "apply the patch", "created_at": "2026-04-07T09:00:00+00:00"},
        {"role": "tool", "name": "run_shell", "args": {"command": "patch -p1"},
         "content": "exit_code: 1\nstdout:\napplied 1 of 3 hunks", "created_at": "2026-04-07T09:01:00+00:00"},
    ] + _history(4)[1:]
    events = [
        {
            "event": "tool_executed",
            "sequence": 1,
            "name": "run_shell",
            "args": {"command": "patch -p1"},
            "result": "exit_code: 1\napplied 1 of 3 hunks",
            "tool_status": "partial_success",
            "tool_error_code": "tool_partial_success",
            "affected_paths": ["moss/parser.py"],
        }
    ]

    artifact, _ = compact(history, events, keep_recent=1)

    assert not any("patch -p1" in entry for entry in artifact.completed)
    assert any("partial_success" in question for question in artifact.open_questions)
    rendered = render_compaction(artifact, 2000)
    assert "partial_success" in rendered


def test_denied_actions_land_in_excluded_with_evidence():
    history = [
        {"role": "user", "content": "delete the workflow", "created_at": "2026-04-07T09:00:00+00:00"},
        {"role": "tool", "name": "write_file", "args": {"path": ".github/workflows/ci.yml"},
         "content": "error: policy refused", "created_at": "2026-04-07T09:01:00+00:00"},
        {"role": "tool", "name": "read_file", "args": {"path": "a.py"},
         "content": "# a.py\n   1: def go(): pass", "created_at": "2026-04-07T09:02:00+00:00"},
    ]
    events = [
        {"event": "tool_executed", "sequence": 1, "name": "write_file",
         "args": {"path": ".github/workflows/ci.yml"}, "result": "error: policy refused",
         "tool_status": "rejected", "tool_error_code": "capability_denied", "affected_paths": []},
        {"event": "tool_executed", "sequence": 2, "name": "read_file", "args": {"path": "a.py", "start": 1},
         "result": "# a.py\n   1: def go(): pass", "tool_status": "ok", "tool_error_code": "",
         "affected_paths": []},
    ]

    artifact, _ = compact(history, events, keep_recent=0)

    assert any("capability_denied" in entry for entry in artifact.excluded)
    assert any(finding.evidence == "a.py:1" for finding in artifact.findings)
    assert all(finding.evidence for finding in artifact.findings)
    assert "do not retry" in render_compaction(artifact, 2000)


def test_successful_writes_become_completed_entries():
    history = [
        {"role": "user", "content": "add the helper", "created_at": "2026-04-07T09:00:00+00:00"},
        {"role": "tool", "name": "write_file", "args": {"path": "moss/helper.py"},
         "content": "wrote moss/helper.py (42 chars)", "created_at": "2026-04-07T09:01:00+00:00"},
    ]
    events = [
        {"event": "tool_executed", "sequence": 3, "name": "write_file", "args": {"path": "moss/helper.py"},
         "result": "wrote moss/helper.py", "tool_status": "ok", "tool_error_code": "",
         "affected_paths": ["moss/helper.py"]},
    ]

    artifact, _ = compact(history, events, keep_recent=0)

    assert artifact.completed == ("write_file moss/helper.py -> changed moss/helper.py",)
    assert artifact.covered_seq_end == 3


def test_nothing_to_compact_returns_no_artifact():
    history = _history(steps=2)

    artifact, remaining = compact(history, _events(history))

    assert artifact is None
    assert remaining == history


def test_runtime_compaction_is_reversible_through_read_artifact(tmp_path):
    agent = build_agent(tmp_path, compaction_mode="rule")
    outputs = ['<tool>{"name":"list_files","args":{"path":"."}}</tool>' for _ in range(6)]
    agent.model_client.outputs = [*outputs, "<final>done</final>"]
    agent.ask("look around")
    for index in range(8):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": f"moss/module_{index}.py"},
                "content": f"# moss/module_{index}.py\n   1: def handler(): pass",
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )

    artifact = agent.compact_context(trigger="test")

    assert artifact is not None
    assert artifact.raw_path.startswith("context/turns-")
    stored = (agent.run_store.run_dir(agent.current_task_state.run_id) / artifact.raw_path)
    assert stored.exists()
    lines = [json.loads(line) for line in stored.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == artifact.covered_history_count

    # 摘要留在历史开头，原文可以按行区间取回。
    assert agent.session["history"][0]["compaction"] == artifact.id
    recovered = agent.run_tool("read_artifact", {"path": artifact.raw_path, "start": 1, "end": 3})
    assert "module_" in recovered or "list_files" in recovered


def test_runtime_compaction_is_idempotent_over_an_already_compacted_range(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"], compaction_mode="rule")
    agent.ask("start")
    for index in range(8):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": f"moss/module_{index}.py"},
                "content": f"# moss/module_{index}.py\n   1: def handler(): pass",
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )

    first = agent.compact_context(trigger="test")
    second = agent.compact_context(trigger="test")

    assert first is not None
    assert second is None
    assert len(agent.session["compactions"]) == 1


def test_compaction_is_off_by_default(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])
    for index in range(8):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": f"m{index}.py"},
                "content": "# m.py\n   1: pass",
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )
    agent.ask("start")

    assert agent.compaction_mode == "off"
    assert agent.compact_context(trigger="test") is None
    assert "compactions" not in agent.session


def test_compaction_triggers_on_context_pressure_and_reaches_the_trace(tmp_path):
    agent = build_agent(tmp_path, compaction_mode="rule")
    agent.model_client.outputs = [
        '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
        "<final>done</final>",
    ]
    for index in range(12):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": f"moss/module_{index}.py"},
                "content": ("# moss/module.py\n" + "   1: def handler(): pass\n" * 200),
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )
    # 窗口设小，让占用率立刻越过阈值。
    agent.context_manager.total_budget = 3000

    agent.ask("keep going")

    events = [event for event in agent.run_store.read_trace(agent.current_task_state.run_id)]
    compacted = [event for event in events if event["event"] == "context_compacted"]
    assert compacted
    assert compacted[0]["trigger"] in {"context_utilization", "history_reduction", "context_overflow"}
    assert compacted[0]["schema_version"] == COMPACTION_SCHEMA_VERSION
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["compaction_mode"] == "rule"
    assert report["compactions"]


def test_oversized_request_is_offloaded_before_giving_up(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"], compaction_mode="rule")

    final = agent.ask("q" * 200000)

    events = [event["event"] for event in agent.run_store.read_trace(agent.current_task_state.run_id)]
    assert "request_offloaded" in events
    assert final == "done"
    run_dir = agent.run_store.run_dir(agent.current_task_state.run_id)
    assert list((run_dir / "artifacts").glob("*user_request*.txt"))
    assert 'read_artifact("artifacts/' in agent.model_client.prompts[-1]


class _AuxClient:
    """一个只会按脚本回话的辅助后端。"""

    def __init__(self, response):
        self.response = response
        self.prompts = []

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _denied_case():
    history = [
        {"role": "user", "content": "wire up the parser", "created_at": "2026-04-07T09:00:00+00:00"},
        {"role": "tool", "name": "write_file", "args": {"path": "moss/helper.py"},
         "content": "wrote moss/helper.py", "created_at": "2026-04-07T09:01:00+00:00"},
        {"role": "tool", "name": "read_file", "args": {"path": "a.py"},
         "content": "# a.py\n   1: def go(): pass", "created_at": "2026-04-07T09:02:00+00:00"},
    ]
    events = [
        {"event": "tool_executed", "sequence": 1, "name": "write_file", "args": {"path": "moss/helper.py"},
         "result": "wrote moss/helper.py", "tool_status": "ok", "tool_error_code": "",
         "affected_paths": ["moss/helper.py"]},
        {"event": "tool_executed", "sequence": 2, "name": "read_file", "args": {"path": "a.py", "start": 1},
         "result": "# a.py\n   1: def go(): pass", "tool_status": "ok", "tool_error_code": "",
         "affected_paths": []},
    ]
    return history, events


def test_model_mode_uses_the_aux_answer_when_it_parses():
    history, events = _denied_case()
    aux = _AuxClient(
        "Sure, here you go:\n"
        + json.dumps(
            {
                "goals": ["wire up the parser end to end"],
                "completed": ["write_file moss/helper.py -> changed moss/helper.py"],
                "excluded": [],
                "findings": [{"text": "a.py only defines go()", "evidence": "a.py:1"}],
                "open_questions": ["is go() still called anywhere?"],
            }
        )
    )

    artifact, _ = compact(history, events, method="model", aux_client=aux, keep_recent=0)

    assert artifact.method == "model"
    assert artifact.goals == ("wire up the parser end to end",)
    assert artifact.findings[0].text == "a.py only defines go()"
    assert artifact.open_questions == ("is go() still called anywhere?",)
    assert "Rule-based draft" in aux.prompts[0]


def test_model_mode_falls_back_to_rule_when_the_answer_is_unusable():
    history, events = _denied_case()

    for response in ("not json at all", "", RuntimeError("aux backend down")):
        artifact, _ = compact(
            history, events, method="model", aux_client=_AuxClient(response), keep_recent=0
        )
        rule_artifact, _ = compact(history, events, keep_recent=0)
        assert artifact.method == "rule"
        assert artifact.fingerprint() == rule_artifact.fingerprint()


def test_model_mode_without_an_aux_client_is_honestly_labelled_rule():
    history, events = _denied_case()

    artifact, _ = compact(history, events, method="model", aux_client=None, keep_recent=0)

    assert artifact.method == "rule"


def test_model_mode_cannot_invent_evidence_or_completions():
    history, events = _denied_case()
    aux = _AuxClient(
        json.dumps(
            {
                "goals": ["ship it"],
                "completed": ["shipped the whole feature", "migrated the database"],
                "excluded": [],
                "findings": [
                    {"text": "everything works", "evidence": "imaginary.py:99"},
                    {"text": "a.py only defines go()", "evidence": "a.py:1"},
                ],
                "open_questions": [],
            }
        )
    )

    artifact, _ = compact(history, events, method="model", aux_client=aux, keep_recent=0)

    # 编造的 completion 和编造的证据锚点都被丢掉，只留下规则模式认得的那些。
    assert artifact.completed == ()
    assert [finding.evidence for finding in artifact.findings] == ["a.py:1"]


def test_model_mode_cannot_promote_a_partial_success_to_completed():
    history = [
        {"role": "user", "content": "apply the patch", "created_at": "2026-04-07T09:00:00+00:00"},
        {"role": "tool", "name": "run_shell", "args": {"command": "patch -p1"},
         "content": "exit_code: 1\napplied 1 of 3 hunks", "created_at": "2026-04-07T09:01:00+00:00"},
    ]
    events = [
        {"event": "tool_executed", "sequence": 1, "name": "run_shell", "args": {"command": "patch -p1"},
         "result": "exit_code: 1\napplied 1 of 3 hunks", "tool_status": "partial_success",
         "tool_error_code": "tool_partial_success", "affected_paths": ["moss/parser.py"]},
    ]
    aux = _AuxClient(
        json.dumps(
            {
                "goals": ["apply the patch"],
                "completed": ["run_shell patch -p1 -> changed moss/parser.py"],
                "excluded": [],
                "findings": [],
                "open_questions": [],
            }
        )
    )

    artifact, _ = compact(history, events, method="model", aux_client=aux, keep_recent=0)

    assert artifact.completed == ()
    assert any("partial_success" in question for question in artifact.open_questions)


def test_runtime_model_mode_uses_the_configured_aux_client(tmp_path):
    aux = _AuxClient(
        json.dumps(
            {
                "goals": ["explore the repo"],
                "completed": [],
                "excluded": [],
                "findings": [],
                "open_questions": ["which module owns parsing?"],
            }
        )
    )
    agent = build_agent(
        tmp_path, ["<final>done</final>"], compaction_mode="model", aux_model_client=aux
    )
    agent.ask("start")
    for index in range(8):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": f"moss/module_{index}.py"},
                "content": f"# moss/module_{index}.py\n   1: def handler(): pass",
                "created_at": f"2026-04-07T09:{index:02d}:00+00:00",
            }
        )

    artifact = agent.compact_context(trigger="test")

    assert artifact.method == "model"
    assert aux.prompts
    assert "which module owns parsing?" in agent.session["history"][0]["content"]
