"""admission gate：超预算就不许发（spec-06 §4.7）。

过去超预算只在 metadata 里标一个 `over_budget_unrecoverable` 然后照发，
真正的反馈来自 provider 的 400——那时这一轮的钱和时间已经花掉了。
这些测试守的是"本地就拒发、原因说得清、退出码非 0"。
"""

import io

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.context_manager import ContextManager
from moss.task_state import STATUS_FAILED, STOP_REASON_CONTEXT_OVERFLOW


class CountingModelClient(FakeModelClient):
    """记录被调用了几次的假后端。gate 生效时这个计数必须是 0。"""

    def __init__(self, outputs):
        super().__init__(outputs)
        self.calls = 0

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.calls += 1
        return super().complete(prompt, max_new_tokens, **kwargs)


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".moss" / "sessions")
    return Moss(
        model_client=CountingModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def _starve_the_budget(agent):
    """把总预算压到连稳定前缀都装不下。

    这是"卸载当前请求也救不回来"的那一档：spec-06 §4.7 的第 1、2 步都用不上，
    只剩第 3 步的硬闸。
    """
    agent.context_manager.total_budget = 40
    return agent


def test_unsendable_prompt_never_reaches_the_provider(tmp_path):
    agent = _starve_the_budget(build_agent(tmp_path, ["<final>never asked</final>"]))

    final = agent.ask("anything at all")

    assert agent.model_client.calls == 0
    assert "Stopped without calling the model" in final
    assert agent.current_task_state.status == STATUS_FAILED
    assert agent.current_task_state.stop_reason == STOP_REASON_CONTEXT_OVERFLOW


def test_context_overflow_writes_full_artifacts_and_a_readable_reason(tmp_path):
    agent = _starve_the_budget(build_agent(tmp_path, ["<final>never asked</final>"]))
    stderr = io.StringIO()
    messages = []
    agent.progress_observer = lambda event, payload: messages.append((event, payload))

    agent.ask("y" * 400)

    task_state = agent.current_task_state
    report = agent.run_store.load_report(task_state.run_id)
    assert report["stop_reason"] == STOP_REASON_CONTEXT_OVERFLOW
    events = [event["event"] for event in agent.run_store.read_trace(task_state.run_id)]
    assert "context_overflow" in events
    assert "run_finished" in events
    overflow = next(
        event for event in agent.run_store.read_trace(task_state.run_id) if event["event"] == "context_overflow"
    )
    assert overflow["reason"] == "request_too_large"
    assert "context budget" in overflow["detail"]

    errors = [payload for event, payload in messages if event == "error"]
    assert errors and errors[0]["scope"] == "context"
    from moss.cli import make_progress_printer

    printer = make_progress_printer(stderr)
    printer("error", errors[0])
    assert "context budget" in stderr.getvalue()


def test_one_shot_exit_code_is_non_zero_on_context_overflow(tmp_path, monkeypatch):
    from moss import cli

    agent = _starve_the_budget(build_agent(tmp_path, ["<final>never asked</final>"]))
    monkeypatch.setattr(cli, "build_agent", lambda args: agent)
    code = cli.main(["--cwd", str(tmp_path), "hello"])

    assert code == 1
    assert agent.model_client.calls == 0


def test_zero_budget_relevant_memory_renders_empty(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.memory.append_note("deploy key lives in vault", tags=("deploy",), created_at="2026-04-07T10:00:00+00:00")

    manager = ContextManager(
        agent,
        total_budget=4000,
        section_budgets={"prefix": 2000, "memory": 500, "relevant_memory": 0, "history": 1000},
        section_floors={"relevant_memory": 0},
        measure=len,
    )
    prompt, metadata = manager.build("where is the deploy key?")

    assert metadata["sections"]["relevant_memory"]["rendered_chars"] == 0
    assert "Relevant memory:" not in prompt
    assert "deploy key lives in vault" not in prompt


def test_build_result_reports_sendable_for_a_normal_prompt(tmp_path):
    agent = build_agent(tmp_path, [])

    result = ContextManager(agent).build_result("small request")

    assert result.sendable is True
    assert result.overflow_reason is None
    assert result.metadata["sendable"] is True


def test_admission_gate_still_applies_when_context_reduction_is_off(tmp_path):
    agent = _starve_the_budget(
        build_agent(tmp_path, ["<final>never asked</final>"], feature_flags={"context_reduction": False})
    )

    final = agent.ask("q" * 400)

    assert agent.model_client.calls == 0
    assert "Stopped without calling the model" in final


def test_an_oversized_request_is_offloaded_rather_than_refused(tmp_path):
    """请求太大先卸载再试（§4.7 第 2 步），拒发是最后一档，不是第一反应。"""
    agent = build_agent(tmp_path, ["<final>read it back</final>"])

    final = agent.ask("x" * (1024 * 1024))

    assert agent.model_client.calls == 1
    assert final == "read it back"
    assert 'read_artifact("artifacts/' in agent.model_client.prompts[-1]
