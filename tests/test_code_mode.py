"""代码执行式工具编排（spec-09 §9.3）。

这是整份 spec 里风险最高的一项，所以测试的重心在**拒绝**上：
AST 白名单要挡住全部逃逸样例（验收要求 ≥20 条），沙箱是硬前置，
默认必须是关的。
"""

import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.extensions.code_mode import (
    MAX_SCRIPT_CHARS,
    CodeModeError,
    render_result,
    run_script,
    sandbox_ready,
    validate_script,
)
from moss.execution.safety.sandbox import SandboxPlan

# 逃逸样例集。每一条都是一条真实存在的提权路径，或者通往它的第一步。
# 白名单的意义就在这里：这份名单不可能穷尽，所以正确的做法不是逐条封堵，
# 而是只放行明确列出的东西。
ESCAPE_SAMPLES = [
    "import os",
    "from os import system",
    "__import__('os').system('id')",
    "eval('1+1')",
    "exec('x = 1')",
    "open('/etc/passwd').read()",
    "getattr(fs, 'read')('x')",
    "().__class__.__mro__[1].__subclasses__()",
    "fs.__class__",
    "fs.__dict__",
    "[c for c in ().__class__.__base__.__subclasses__()]",
    "__builtins__",
    "globals()",
    "locals()",
    "vars(fs)",
    "dir(fs)",
    "type(fs)",
    "compile('1', 'x', 'eval')",
    "def helper():\n    return 1",
    "lambda: 1",
    "class Evil:\n    pass",
    "try:\n    pass\nexcept Exception:\n    pass",
    "with open('x') as handle:\n    pass",
    "yield 1",
    "async def go():\n    pass",
    "assert False, 'x'",
    "del fs",
    "global fs",
    "raise SystemExit",
    "x = '__' + 'class__'",
    "fs.read.__globals__",
    "_secret = 1",
]


def _agent(tmp_path, *, code_mode=False, sandbox="off", **kwargs):
    (tmp_path / "README.md").write_text("hello TODO\n", encoding="utf-8")
    (tmp_path / "other.md").write_text("nothing here\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        code_mode=code_mode,
        sandbox=sandbox,
        **kwargs,
    )


# --- AST 白名单 ---------------------------------------------------------


@pytest.mark.parametrize("source", ESCAPE_SAMPLES, ids=range(len(ESCAPE_SAMPLES)))
def test_every_escape_sample_is_rejected(source):
    with pytest.raises(CodeModeError):
        validate_script(source)


def test_the_escape_corpus_meets_the_acceptance_size():
    """spec-09 §9.3 验收：AST 白名单要拒绝 ≥20 条逃逸样例。"""
    assert len(ESCAPE_SAMPLES) >= 20


def test_a_plain_orchestration_script_validates():
    validate_script(
        "hits = []\n"
        "for path in ['README.md', 'other.md']:\n"
        "    text = fs.read(path)\n"
        "    if 'TODO' in text:\n"
        "        hits.append(path)\n"
        "emit(', '.join(hits))\n"
    )


def test_empty_scripts_are_rejected():
    with pytest.raises(CodeModeError, match="must not be empty"):
        validate_script("   \n")


def test_oversized_scripts_are_rejected():
    with pytest.raises(CodeModeError, match="longer than"):
        validate_script("emit('x')\n" * MAX_SCRIPT_CHARS)


def test_syntax_errors_are_reported_as_validation_failures():
    with pytest.raises(CodeModeError, match="does not parse"):
        validate_script("for x in :\n")


def test_unlisted_attributes_are_rejected():
    with pytest.raises(CodeModeError, match="attribute access is not allowed"):
        validate_script("fs.delete('README.md')")


# --- 执行 ---------------------------------------------------------------


def _recorder():
    calls = []

    def run_tool(name, args):
        calls.append((name, dict(args)))
        return "TODO here" if args.get("path") == "README.md" else "clean"

    return run_tool, calls


def test_script_batches_tool_calls_through_the_guarded_entry_point():
    run_tool, calls = _recorder()

    emitted, used = run_script(
        "for path in ['README.md', 'other.md']:\n"
        "    if 'TODO' in fs.read(path):\n"
        "        emit(path)\n",
        run_tool,
    )

    assert emitted == ["README.md"]
    assert used == ["read_file", "read_file"]
    assert [name for name, _ in calls] == ["read_file", "read_file"]


def test_search_and_ls_share_the_same_call_budget():
    """每个入口各持一份预算的话，循环里换个入口就能绕过上限。"""
    run_tool, _ = _recorder()

    with pytest.raises(CodeModeError, match="exceeded 3 tool calls"):
        run_script(
            "for index in range(10):\n    ls('.')\n    search('x')\n", run_tool, max_tool_calls=3
        )


def test_script_errors_are_converted_not_raised_raw():
    def run_tool(name, args):
        raise RuntimeError("tool blew up")

    with pytest.raises(CodeModeError, match="RuntimeError"):
        run_script("fs.read('README.md')", lambda name, args: run_tool(name, args))


def test_timeout_is_reported():
    def run_tool(name, args):
        import time

        time.sleep(2)
        return ""

    with pytest.raises(CodeModeError, match="did not finish"):
        run_script("for index in range(10):\n    fs.read('x')\n", run_tool, timeout=0.5)


def test_an_infinite_loop_is_actually_terminated():
    """死循环过得了 AST 白名单（它不是逃逸）。放着不管就是烧一个核到进程退出。"""
    import threading

    before = threading.active_count()
    with pytest.raises(CodeModeError, match="time budget"):
        run_script("while True:\n    pass\n", lambda name, args: "", timeout=1)

    assert threading.active_count() <= before


def test_safe_builtins_are_available():
    emitted, _ = run_script("emit(len(sorted([3, 1, 2])))", lambda name, args: "")

    assert emitted == [3]


def test_render_result_names_the_calls():
    text = render_result(["a"], ["read_file", "search_text"])

    assert "2 tool call(s): read_file, search_text" in text
    assert "a" in text


def test_render_result_nudges_when_nothing_was_emitted():
    assert "emitted nothing" in render_result([], ["read_file"])


# --- 沙箱硬前置 ---------------------------------------------------------


def test_sandbox_none_is_not_ready():
    assert sandbox_ready(SandboxPlan(mode="none")) is False
    assert sandbox_ready(None) is False


def test_a_real_sandbox_is_ready():
    assert sandbox_ready(SandboxPlan(mode="bwrap")) is True


def test_tool_is_absent_by_default(tmp_path):
    assert "run_orchestration" not in _agent(tmp_path).tools


def test_tool_stays_absent_without_a_sandbox(tmp_path, capsys):
    """没有沙箱就不给这个工具，哪怕开关开着。"""
    agent = _agent(tmp_path, code_mode=True, sandbox="off")

    assert "run_orchestration" not in agent.tools
    assert "no sandbox is available" in capsys.readouterr().err


def test_tool_appears_with_a_sandbox(tmp_path, monkeypatch):
    from moss.execution.safety import sandbox as sandboxlib

    monkeypatch.setattr(sandboxlib, "detect", lambda requested="auto", platform=None: SandboxPlan(mode="bwrap"))
    agent = _agent(tmp_path, code_mode=True, sandbox="auto")

    assert "run_orchestration" in agent.tools
    assert agent.tools["run_orchestration"]["risky"] is True


def test_end_to_end_orchestration_replaces_several_round_trips(tmp_path, monkeypatch):
    from moss.execution.safety import sandbox as sandboxlib

    monkeypatch.setattr(sandboxlib, "detect", lambda requested="auto", platform=None: SandboxPlan(mode="bwrap"))
    agent = _agent(tmp_path, code_mode=True, sandbox="auto")

    result = agent.run_tool(
        "run_orchestration",
        {
            "script": "for path in ['README.md', 'other.md']:\n"
            "    if 'TODO' in fs.read(path):\n"
            "        emit(path)\n"
        },
    )

    assert "README.md" in result
    assert "2 tool call(s)" in result


def test_escape_scripts_are_rejected_at_validation_time(tmp_path, monkeypatch):
    """一段逃逸脚本该在审批摘要出现之前就被拒掉，不该让用户对着它按 y。"""
    from moss.execution.safety import sandbox as sandboxlib

    monkeypatch.setattr(sandboxlib, "detect", lambda requested="auto", platform=None: SandboxPlan(mode="bwrap"))
    agent = _agent(tmp_path, code_mode=True, sandbox="auto")

    result = agent.run_tool("run_orchestration", {"script": "__import__('os').system('id')"})

    assert "invalid arguments" in result


def test_orchestrated_calls_still_hit_the_guardrails(tmp_path, monkeypatch):
    """脚本只是把多次调用打包，不是绕过护栏的旁路。"""
    from moss.execution.safety import sandbox as sandboxlib

    monkeypatch.setattr(sandboxlib, "detect", lambda requested="auto", platform=None: SandboxPlan(mode="bwrap"))
    agent = _agent(tmp_path, code_mode=True, sandbox="auto")

    result = agent.run_tool("run_orchestration", {"script": "emit(fs.read('../../etc/passwd'))"})

    assert "path escapes workspace" in result
