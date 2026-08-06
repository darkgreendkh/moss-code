"""多模型路由（spec-09 §9.7）。

守三条：未配置 aux 时行为完全不变、aux 输出不进主线 history、
aux 失败自动回落主模型并且这件事在 report 里看得见。
"""

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.runs.observability import events as trace_events
from moss.extensions.router import AUX_TASKS, ModelRouter


class _Client(FakeModelClient):
    def __init__(self, name, outputs=None):
        super().__init__(outputs or [])
        self.provider = name
        self.model = name
        self.calls = []

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.calls.append(prompt)
        return f"{self.model}-said-something"


class _BrokenClient(_Client):
    def complete(self, prompt, max_new_tokens, **kwargs):
        self.calls.append(prompt)
        raise RuntimeError("aux backend is down")


def _agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


# --- 路由决策 -----------------------------------------------------------


def test_aux_tasks_go_to_the_aux_model():
    main, aux = _Client("main"), _Client("aux")
    router = ModelRouter(main, aux)

    for task_kind in sorted(AUX_TASKS):
        assert router.route(task_kind) is aux


def test_main_line_never_goes_to_the_aux_model():
    main, aux = _Client("main"), _Client("aux")
    router = ModelRouter(main, aux)

    assert router.route("agent_step") is main


def test_without_an_aux_model_everything_falls_back_to_main():
    """未配置 aux = 消融基线，行为必须与加路由前逐字节一致。"""
    main = _Client("main")
    router = ModelRouter(main, None)

    assert all(router.route(kind) is main for kind in sorted(AUX_TASKS) + ["agent_step"])
    assert router.summary()["aux_configured"] is False


def test_every_route_is_observed():
    seen = []
    router = ModelRouter(_Client("main"), _Client("aux"), observer=seen.append)

    router.route("compaction")

    assert seen == [
        {"task_kind": "compaction", "route": "aux", "model": "aux", "provider": "aux", "reason": "aux task"}
    ]


def test_a_broken_observer_cannot_break_routing():
    def explode(record):
        raise RuntimeError("observer is buggy")

    router = ModelRouter(_Client("main"), _Client("aux"), observer=explode)

    assert router.route("compaction") is not None


# --- 失败回落 -----------------------------------------------------------


def test_aux_failure_falls_back_to_the_main_model():
    main, aux = _Client("main"), _BrokenClient("aux")
    router = ModelRouter(main, aux)

    result = router.call("compaction", lambda client: client.complete("x", 10))

    assert result == "main-said-something"
    assert aux.calls and main.calls
    assert router.aux_degraded is True


def test_main_model_failure_still_raises():
    """脏活的回落只到主模型为止；主模型也炸了就是真的炸了。"""
    router = ModelRouter(_BrokenClient("main"), None)

    try:
        router.call("compaction", lambda client: client.complete("x", 10))
    except RuntimeError as exc:
        assert "down" in str(exc)
    else:
        raise AssertionError("expected the main model failure to propagate")


def test_bound_client_hides_routing_from_the_subsystem():
    main, aux = _Client("main"), _Client("aux")
    router = ModelRouter(main, aux)

    client = router.bind("compaction")

    assert client.model == "aux"
    assert client.complete("summarize this", 100) == "aux-said-something"
    assert not main.calls


def test_bound_client_falls_back_transparently():
    main, aux = _Client("main"), _BrokenClient("aux")

    assert ModelRouter(main, aux).bind("compaction").complete("x", 10) == "main-said-something"


# --- 与 runtime 的接线 --------------------------------------------------


def test_report_records_routing_and_degradation(tmp_path):
    aux = _BrokenClient("aux")
    # 主 client 要留一手输出：aux 炸掉之后回落主模型，主模型也得答得出来。
    agent = _agent(tmp_path, ["<final>done</final>", "summary"], aux_model_client=aux)

    agent.ask("hello")
    agent.aux_model_client("compaction").complete("summarize", 50)

    routing = agent.model_router.summary()
    assert routing["aux_configured"] is True
    assert routing["aux_degraded"] is True
    assert routing["aux_calls"] >= 1


def test_report_shows_no_aux_when_unconfigured(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"])

    agent.ask("hello")

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["model_routing"]["aux_configured"] is False
    assert report["model_routing"]["aux_degraded"] is False


def test_routing_lands_in_the_trace(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"], aux_model_client=_Client("aux"), compaction_mode="rule")
    agent.ask("hello")

    # 路由发生在真正调用的时候，不在拿 client 的时候——拿了不用不该记一笔。
    agent.aux_model_client("compaction").complete("summarize", 50)

    routed = [
        event
        for event in agent.run_store.read_trace(agent.current_task_state.run_id)
        if event["event"] == trace_events.MODEL_ROUTED
    ]
    assert routed and routed[-1]["task_kind"] == "compaction"


def test_aux_output_never_enters_the_main_history(tmp_path):
    """小模型的措辞混进主线推理链是最难查的一类退化。"""
    aux = _Client("aux")
    agent = _agent(tmp_path, ["<final>done</final>"], aux_model_client=aux)

    agent.ask("hello")
    agent.aux_model_client("reflection").complete("rewrite this", 50)

    history = "\n".join(str(item.get("content", "")) for item in agent.session["history"])
    assert "aux-said-something" not in history


def test_reflection_uses_the_aux_model_when_configured(tmp_path):
    aux = _Client("aux")
    agent = _agent(tmp_path, ["<final>done</final>"], aux_model_client=aux, reflect_mode="model")

    assert agent._reflection_summarizer() is not None


def test_reflection_does_not_call_a_model_without_an_aux_backend(tmp_path):
    """没配 aux 就别为了改写措辞再打一次主模型——那是纯成本。"""
    agent = _agent(tmp_path, ["<final>done</final>"], reflect_mode="model")

    assert agent._reflection_summarizer() is None


# --- CLI ----------------------------------------------------------------


def test_cli_without_aux_flags_builds_no_aux_client(tmp_path):
    from moss.cli import build_agent, build_arg_parser

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    assert build_agent(args).model_router.aux_client is None


def test_cli_aux_flags_build_a_separate_client(tmp_path):
    from moss.cli import build_agent, build_arg_parser

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    args = build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--aux-provider", "ollama", "--aux-model", "qwen3:8b"]
    )

    agent = build_agent(args)

    assert agent.model_router.aux_client is not agent.model_client
    assert agent.model_router.aux_client.model == "qwen3:8b"
    assert agent.model_router.aux_client.provider == "ollama"
