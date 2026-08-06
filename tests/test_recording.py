"""确定性录制回放（spec-09 §9.8）。

这些测试守三件事：指纹对"语义相同"的请求稳定、回放逐字节一致、
磁带落盘前过脱敏。第三条最要紧——磁带是要进 git 的。
"""


import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.runs.observability import events as trace_events
from moss.model_request import Block, Message, ModelRequest
from moss.providers.recording import (
    Cassette,
    RecordingModelClient,
    ReplayMiss,
    ReplayModelClient,
    request_fingerprint,
)


def _request(text="hello", *, system="you are moss", tools=(), max_new_tokens=64):
    return ModelRequest(
        system=(Block(text=system, kind="prefix"),),
        messages=(Message(role="user", blocks=(Block(text=text, kind="request"),)),),
        tools=tuple(tools),
        max_new_tokens=max_new_tokens,
    )


def _agent(tmp_path, client, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


# --- 指纹规范化 ---------------------------------------------------------


def test_fingerprint_is_stable_for_the_same_request():
    assert request_fingerprint(_request()) == request_fingerprint(_request())


def test_fingerprint_ignores_timestamps_and_run_ids():
    left = _request("started run_20260806-101112-a1b2c3 at 2026-08-06T10:11:12Z")
    right = _request("started run_20260101-000000-ffffff at 2026-01-01T00:00:00Z")

    assert request_fingerprint(left) == request_fingerprint(right)


def test_fingerprint_ignores_the_workspace_root_prefix():
    left = _request("read /tmp/moss-benchmark-aaa/repo/README.md")
    right = _request("read /tmp/moss-benchmark-bbb/repo/README.md")

    assert request_fingerprint(left, "/tmp/moss-benchmark-aaa/repo") == request_fingerprint(
        right, "/tmp/moss-benchmark-bbb/repo"
    )


def test_fingerprint_separates_semantically_different_requests():
    """规范化剔得太狠会让两个不同的请求撞成一把钥匙，回放出错误的回答。"""
    assert request_fingerprint(_request("read README.md")) != request_fingerprint(
        _request("delete README.md")
    )
    assert request_fingerprint(_request()) != request_fingerprint(_request(system="you are not moss"))
    assert request_fingerprint(_request()) != request_fingerprint(_request(max_new_tokens=128))
    assert request_fingerprint(_request()) != request_fingerprint(
        _request(tools=({"name": "read_file"},))
    )


def test_call_id_does_not_change_the_fingerprint():
    """call_id 是 provider 每次新生成的随机串，进指纹等于永远不命中。"""
    left = ModelRequest(messages=(Message(role="assistant", blocks=(Block(text="x", kind="tool_call"),), call_id="a"),))
    right = ModelRequest(messages=(Message(role="assistant", blocks=(Block(text="x", kind="tool_call"),), call_id="b"),))

    assert request_fingerprint(left) == request_fingerprint(right)


# --- 录制 / 回放 --------------------------------------------------------


def test_recording_writes_a_manifest_and_one_file_per_call(tmp_path):
    client = RecordingModelClient(FakeModelClient(["first", "second"]), tmp_path / "cassette")

    client.complete_request(_request("a"))
    client.complete_request(_request("b"))

    cassette = Cassette(tmp_path / "cassette")
    entries = cassette.entries()
    assert [entry["response"] for entry in entries] == ["first", "second"]
    assert cassette.read_manifest()["provider"] == "fake"
    assert [path.name[:3] for path in cassette.entry_paths()] == ["000", "001"]


def test_recording_keeps_the_inner_client_identity(tmp_path):
    """provider/model 必须透传：包一层之后 report 记的还得是真后端。"""
    inner = FakeModelClient(["x"])
    inner.provider, inner.model = "deepseek", "deepseek-v4-pro"

    client = RecordingModelClient(inner, tmp_path / "cassette")

    assert (client.provider, client.model) == ("deepseek", "deepseek-v4-pro")


def test_replay_returns_the_recorded_response(tmp_path):
    recorder = RecordingModelClient(FakeModelClient(["answer"]), tmp_path / "cassette")
    recorder.complete_request(_request("q"))

    player = ReplayModelClient(tmp_path / "cassette")

    assert player.complete_request(_request("q")) == "answer"


def test_replay_serves_repeat_requests_in_recorded_order(tmp_path):
    """同一个 prompt 被问两次，回放要按录制顺序给两个不同的回答。"""
    recorder = RecordingModelClient(FakeModelClient(["first", "second"]), tmp_path / "cassette")
    recorder.complete_request(_request("q"))
    recorder.complete_request(_request("q"))

    player = ReplayModelClient(tmp_path / "cassette")

    assert [player.complete_request(_request("q")) for _ in range(3)] == ["first", "second", "second"]


def test_replay_is_byte_identical_across_repeats(tmp_path):
    recorder = RecordingModelClient(FakeModelClient([f"answer-{index}" for index in range(3)]), tmp_path / "cassette")
    for index in range(3):
        recorder.complete_request(_request(f"q{index}"))

    outputs = []
    for _ in range(10):
        player = ReplayModelClient(tmp_path / "cassette")
        outputs.append([player.complete_request(_request(f"q{index}")) for index in range(3)])

    assert outputs.count(outputs[0]) == 10


def test_replay_miss_fails_by_default(tmp_path):
    RecordingModelClient(FakeModelClient(["a"]), tmp_path / "cassette").complete_request(_request("q"))

    player = ReplayModelClient(tmp_path / "cassette")

    with pytest.raises(ReplayMiss):
        player.complete_request(_request("something else entirely"))


def test_replay_nearest_warns_instead_of_failing(tmp_path):
    RecordingModelClient(FakeModelClient(["a"]), tmp_path / "cassette").complete_request(_request("q"))
    seen = []
    player = ReplayModelClient(tmp_path / "cassette", on_miss="nearest", miss_observer=seen.append)

    assert player.complete_request(_request("different")) == "a"
    assert seen and seen[0]["on_miss"] == "nearest"


def test_replay_passthrough_falls_back_to_the_real_client(tmp_path):
    inner = FakeModelClient(["live"])
    player = ReplayModelClient(tmp_path / "cassette", on_miss="passthrough", inner=inner)

    assert player.complete_request(_request("q")) == "live"
    assert player.misses


def test_unknown_on_miss_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ReplayModelClient(tmp_path / "cassette", on_miss="whatever")


# --- 脱敏（磁带会进仓库） -----------------------------------------------


def test_cassettes_never_land_a_plaintext_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSS_OPENAI_API_KEY", "super-secret-value-42")
    client = RecordingModelClient(
        FakeModelClient(["the key is sk-abcdefghijklmnopqrstuvwx and super-secret-value-42"]),
        tmp_path / "cassette",
        secret_env_names=("MOSS_OPENAI_API_KEY",),
    )

    client.complete_request(_request("q"))

    text = "".join(path.read_text(encoding="utf-8") for path in Cassette(tmp_path / "cassette").entry_paths())
    assert "super-secret-value-42" not in text
    assert "sk-abcdefghijklmnopqrstuvwx" not in text
    assert "<redacted>" in text


def test_committed_cassettes_carry_no_secret_shapes():
    """仓库里真实存在的磁带也扫一遍——这条断言是防线，不是演示。"""
    from pathlib import Path

    from moss.security import redact_text

    offenders = []
    for path in sorted(Path("benchmarks/cassettes").rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        if redact_text(text, env={}) != text:
            offenders.append(str(path))
    assert not offenders, f"磁带里出现了疑似明文密钥: {offenders}"


# --- 与主循环的接线 -----------------------------------------------------


def test_agent_run_can_be_recorded_and_replayed(tmp_path):
    outputs = ['<tool>{"name":"list_files","args":{"path":"."}}</tool>', "<final>done</final>"]
    recorder = RecordingModelClient(FakeModelClient(list(outputs)), tmp_path / "cassette", root=str(tmp_path / "live"))
    first = _agent(tmp_path / "live", recorder).ask("look around")

    replay_root = tmp_path / "replayed"
    player = ReplayModelClient(tmp_path / "cassette", root=str(replay_root))
    second = _agent(replay_root, player).ask("look around")

    assert first == second == "done"
    assert player.misses == []


def test_replay_miss_lands_in_the_trace(tmp_path):
    RecordingModelClient(FakeModelClient(["<final>a</final>"]), tmp_path / "cassette").complete_request(_request("q"))
    player = ReplayModelClient(tmp_path / "cassette", on_miss="nearest")
    agent = _agent(tmp_path / "work", player)

    agent.ask("a task the cassette never saw")

    trace = agent.run_store.read_trace(agent.current_task_state.run_id)
    assert any(event["event"] == trace_events.REPLAY_MISS for event in trace)


def test_report_marks_a_replayed_run(tmp_path):
    recorder = RecordingModelClient(FakeModelClient(["<final>done</final>"]), tmp_path / "cassette", root=str(tmp_path / "live"))
    _agent(tmp_path / "live", recorder).ask("hello")

    replay_root = tmp_path / "replayed"
    agent = _agent(replay_root, ReplayModelClient(tmp_path / "cassette", root=str(replay_root)))
    agent.ask("hello")

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["replay"]["miss_count"] == 0
    assert report["replay"]["on_miss"] == "fail"


def test_non_replay_run_has_an_empty_replay_summary(tmp_path):
    agent = _agent(tmp_path, FakeModelClient(["<final>done</final>"]))

    agent.ask("hello")

    assert agent.run_store.load_report(agent.current_task_state.run_id)["replay"] == {}


# --- CLI 接线 -----------------------------------------------------------


def test_cli_rejects_recording_and_replaying_at_once(tmp_path):
    from moss.cli import build_agent, build_arg_parser

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    args = build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--record", str(tmp_path / "a"), "--replay", str(tmp_path / "b")]
    )

    with pytest.raises(ValueError):
        build_agent(args)


def test_cli_replay_flag_builds_a_replay_client(tmp_path):
    from moss.cli import build_agent, build_arg_parser

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    RecordingModelClient(FakeModelClient(["x"]), tmp_path / "cassette").complete_request(_request("q"))
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path), "--replay", str(tmp_path / "cassette")])

    agent = build_agent(args)

    assert isinstance(agent.model_client, ReplayModelClient)
    # runtime 接管了 miss 观察者，否则 miss 只会留在 stderr。
    assert agent.model_client.miss_observer == agent.note_replay_miss


# --- L1 消费磁带 --------------------------------------------------------


def test_l1_replaces_at_least_half_of_the_handwritten_scripts():
    """spec-09 §9.8 验收：回放集要顶掉一半以上的 SCRIPTED_MODEL_OUTPUTS。"""
    from pathlib import Path

    from moss.evaluation import cassettes
    from moss.evaluation.evaluator import SCRIPTED_MODEL_OUTPUTS

    recorded = set(cassettes.cassette_task_ids(Path(".")))

    assert recorded, "benchmarks/cassettes/<prompt_version>/ 是空的，跑 scripts/record_cassettes.py --all"
    assert len(recorded) * 2 >= len(SCRIPTED_MODEL_OUTPUTS)
    # 在 benchmark 里、却没有磁带的任务，必须是显式登记过原因的，不能是"忘了录"。
    from moss.evaluation.evaluator import DEFAULT_BENCHMARK_PATH, load_benchmark

    benchmark_ids = {task["id"] for task in load_benchmark(DEFAULT_BENCHMARK_PATH, repo_root=Path("."))["tasks"]}
    assert (benchmark_ids & set(SCRIPTED_MODEL_OUTPUTS)) - recorded == set(cassettes.UNCASSETTABLE_TASKS)


def test_every_committed_cassette_declares_its_source():
    """从脚本引导出来的磁带不能冒充真实模型轨迹。"""
    from pathlib import Path

    from moss.evaluation import cassettes
    from moss.providers.recording import Cassette

    for task_id in cassettes.cassette_task_ids(Path(".")):
        manifest = Cassette(cassettes.cassette_dir(Path("."), task_id)).read_manifest()
        assert manifest.get("source") in {
            cassettes.SOURCE_SCRIPTED_BOOTSTRAP,
            cassettes.SOURCE_PROVIDER,
        }, task_id
        assert manifest.get("prompt_version"), task_id
