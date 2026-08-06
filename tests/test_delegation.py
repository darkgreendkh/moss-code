"""子 agent 上下文隔离（spec-09 §9.1）。

守三条：能力是父集的严格子集、背景是**构造**出来的而不是截断的 history、
回给父 agent 的是带证据锚点的结论摘要而不是子 agent 的裸输出。
"""

import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss import trace_events
from moss.delegation import (
    DELEGATE_CAPABILITIES,
    DelegateContract,
    Finding,
    parse_delegate_output,
)
from moss.token_budget import estimate_tokens


def _agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / "target.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


# --- 契约与能力 ---------------------------------------------------------


def test_delegate_capabilities_are_a_strict_subset_of_the_parent(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"])
    parent = agent.capability_set()

    contract = agent.delegate_contract("look at target.py")

    assert contract.capabilities < parent
    assert contract.capabilities <= DELEGATE_CAPABILITIES


def test_contract_refuses_capability_escalation():
    """父 agent 自己都没有的能力，子 agent 更不能有。"""
    contract = DelegateContract(goal="x", capabilities=frozenset({"fs_read", "exec"}))

    with pytest.raises(ValueError, match="escalate"):
        contract.validate_against(frozenset({"fs_read"}))


def test_contract_refuses_to_leave_read_only():
    """父 agent 有 fs_write 不代表子 agent 可以有。可写子 agent 要等沙箱。"""
    contract = DelegateContract(goal="x", capabilities=frozenset({"fs_read", "fs_write"}))

    with pytest.raises(ValueError, match="read-only"):
        contract.validate_against(frozenset({"fs_read", "fs_write"}))


def test_delegate_tools_are_read_only(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"])

    assert set(agent.delegate_contract("x").allowed_tools) == {"list_files", "read_file", "search_text"}


# --- context_seed 是构造的，不是截断的 history ---------------------------


def test_context_seed_does_not_carry_parent_history(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"])
    agent.record({"role": "user", "content": "SECRET-PARENT-CHATTER " * 40, "created_at": "2026-01-01T00:00:00+00:00"})

    seed = agent.delegate_contract("look at target.py").seed_text()

    assert "SECRET-PARENT-CHATTER" not in seed


def test_context_seed_carries_the_goal_and_relevant_files(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"])
    agent.memory.set_task_summary("refactor the retry path")

    seed = agent.delegate_contract("where is retry handled?", {"focus": ["target.py"]}).seed_text()

    assert "refactor the retry path" in seed
    assert "target.py" in seed


def test_context_seed_is_far_smaller_than_the_parent_history(tmp_path):
    """spec-09 §9.1 验收：委派路径的父上下文投入要明显低于把 history 倒过去。"""
    agent = _agent(tmp_path, ["<final>done</final>"])
    for index in range(30):
        agent.record({"role": "user", "content": f"turn-{index} " + "x" * 400, "created_at": "2026-01-01T00:00:00+00:00"})

    seed_tokens = estimate_tokens(agent.delegate_contract("where is retry handled?").seed_text())
    history_tokens = estimate_tokens(agent.history_text())

    assert seed_tokens < history_tokens * 0.6


# --- 结构化结果 ---------------------------------------------------------


def test_parse_extracts_findings_with_anchors():
    result = parse_delegate_output(
        "FINDING: retry lives in the loop [moss/agent_loop.py:120-140]\n"
        "UNRESOLVED: is the backoff configurable?\n"
        "CONFIDENCE: 0.8",
        goal="retry",
    )

    assert result.findings == (
        Finding(claim="retry lives in the loop", evidence_path="moss/agent_loop.py", line_range=(120, 140)),
    )
    assert result.unresolved == ("is the backoff configurable?",)
    assert result.confidence == 0.8


def test_parse_accepts_a_single_line_anchor():
    result = parse_delegate_output("FINDING: defined here [a.py:12]")

    assert result.findings[0].line_range == (12, 12)


def test_parse_drops_anchors_that_do_not_resolve():
    """假锚点比没锚点更糟：父 agent 会去读，读不到，然后不知道信哪个。"""
    result = parse_delegate_output(
        "FINDING: it is here [../outside.py:1-2]", verify_anchor=lambda path: False
    )

    assert result.findings[0].evidence_path == ""
    assert result.findings[0].claim == "it is here"


def test_parse_degrades_unstructured_output_instead_of_losing_it():
    result = parse_delegate_output("the parser is in output_parser.py, I think")

    assert len(result.findings) == 1
    assert not result.findings[0].anchored
    # 没有出处的结论置信度是 0，父 agent 该自己去核。
    assert result.confidence == 0.0


def test_confidence_defaults_to_the_anchored_ratio():
    result = parse_delegate_output("FINDING: a [a.py:1]\nFINDING: b", verify_anchor=lambda path: True)

    assert result.confidence == 0.5


def test_render_is_a_summary_not_the_raw_child_output():
    result = parse_delegate_output("FINDING: short claim [a.py:1-2]\nCONFIDENCE: 0.9", goal="g")

    rendered = result.render()

    assert "a.py:1-2" in rendered
    assert rendered.startswith("delegate_result (goal: g, confidence: 0.90)")


# --- 端到端 -------------------------------------------------------------


def test_delegate_returns_structured_findings_to_the_parent(tmp_path):
    agent = _agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"task":"what does handle return?","focus":["target.py"]}}</tool>',
            "<final>it returns 1</final>",
        ],
        max_depth=1,
    )
    # 子 agent 复用同一个 client，所以它的输出接在父 agent 的脚本后面。
    agent.model_client.outputs.insert(1, "<final>FINDING: handle returns 1 [target.py:1-2]\nCONFIDENCE: 0.9</final>")

    agent.ask("check target.py")

    delegate_turn = next(
        item for item in agent.session["history"] if "delegate_result" in str(item.get("content", ""))
    )
    assert "handle returns 1 [target.py:1-2]" in delegate_turn["content"]
    assert "cost:" in delegate_turn["content"]


def test_delegate_result_is_much_smaller_than_the_child_transcript(tmp_path):
    """出口治理：子 agent 烧掉的 token 不能原样倒回父 context。"""
    verbose = "\n".join(f"line {index} of rambling analysis" for index in range(200))
    agent = _agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"task":"summarize"}}</tool>',
            f"<final>{verbose}\nFINDING: the answer is 1 [target.py:1-2]\nCONFIDENCE: 0.7</final>",
            "<final>done</final>",
        ],
        max_depth=1,
    )

    agent.ask("investigate")

    delegate_turn = next(
        item for item in agent.session["history"] if "delegate_result" in str(item.get("content", ""))
    )
    assert estimate_tokens(delegate_turn["content"]) < estimate_tokens(verbose) * 0.4


def test_delegate_fan_out_aggregates_in_submission_order(tmp_path):
    agent = _agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"tasks":["question one","question two"]}}</tool>',
            "<final>done</final>",
        ],
        max_depth=1,
    )
    # 两个子 agent 并发取输出，所以脚本里两条都要在；顺序由聚合阶段决定。
    agent.model_client.outputs.insert(1, "<final>FINDING: answer A [target.py:1]</final>")
    agent.model_client.outputs.insert(2, "<final>FINDING: answer B [target.py:2]</final>")

    agent.ask("investigate two things")

    content = next(
        item["content"] for item in agent.session["history"] if "delegate_result" in str(item.get("content", ""))
    )
    assert content.index("goal: question one") < content.index("goal: question two")


def test_delegate_failure_does_not_crash_the_parent(tmp_path):
    """子 agent 内部炸了，父 agent 该拿到一条"这条路没走通"，而不是跟着崩。"""

    class ExplodingClient(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            raise RuntimeError("backend is down")

    agent = _agent(tmp_path, ["<final>done</final>"], max_depth=1)
    agent.model_client = ExplodingClient([])

    result = agent.run_delegate(agent.delegate_contract("investigate"))

    assert result.error
    assert result.findings == ()


def test_delegate_emits_spawn_and_finish_events(tmp_path):
    agent = _agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"task":"look"}}</tool>',
            "<final>FINDING: nothing [target.py:1]</final>",
            "<final>done</final>",
        ],
        max_depth=1,
    )

    agent.ask("investigate")

    events = [event["event"] for event in agent.run_store.read_trace(agent.current_task_state.run_id)]
    assert trace_events.DELEGATE_SPAWNED in events
    assert trace_events.DELEGATE_FINISHED in events


def test_delegate_cost_is_reported_back(tmp_path):
    agent = _agent(tmp_path, ["<final>FINDING: x [target.py:1]</final>"], max_depth=1)
    contract = agent.delegate_contract("look")

    result = agent.run_delegate(contract)

    assert result.cost["steps"] == 0
    assert result.cost["wall_s"] >= 0
