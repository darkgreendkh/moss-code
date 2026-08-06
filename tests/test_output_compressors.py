"""按类型压缩工具输出（spec-06 §4.3）。

压缩是有损的，所以每一类都必须回答同一个问题：这一类输出里，
哪几行是下一步决策真正依赖的？测试守的就是那几行不能被切掉。
"""

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext, output_compressors
from moss.output_compressors import compress, detect_kind, register, registered_kinds
from moss.task_state import TaskState
from moss.tool_executor import prepare_tool_output

PYTEST_OUTPUT = """exit_code: 1
stdout:
============================= test session starts ==============================
collected 412 items

tests/test_alpha.py ....................................................  [ 40%]
tests/test_beta.py .....................................................  [ 80%]
=================================== FAILURES ===================================
_________________________ test_parses_nested_payload __________________________

    def test_parses_nested_payload():
        payload = build_payload()
""" + "\n".join(f"        step_{index}()" for index in range(60)) + """
>       assert parse(payload) == {"ok": True}
E       AssertionError: assert {'ok': False} == {'ok': True}
E         Differing items:
E         {'ok': False} != {'ok': True}

tests/test_alpha.py:118: AssertionError
_________________________ test_retries_on_timeout _____________________________

>       assert retries == 3
E       assert 1 == 3

tests/test_beta.py:44: AssertionError
=========================== short test summary info ============================
FAILED tests/test_alpha.py::test_parses_nested_payload - AssertionError
FAILED tests/test_beta.py::test_retries_on_timeout - assert 1 == 3
========================= 2 failed, 410 passed in 41.2s ========================
"""

RUFF_OUTPUT = "exit_code: 1\nstdout:\n" + "\n".join(
    [f"moss/module_{index}.py:{index + 1}:80: E501 Line too long (120 > 100)" for index in range(120)]
    + [f"moss/other_{index}.py:{index + 1}:1: F401 `os` imported but unused" for index in range(30)]
    + ["Found 150 errors."]
)

GIT_DIFF_OUTPUT = "exit_code: 0\nstdout:\ndiff --git a/moss/alpha.py b/moss/alpha.py\n" + "\n".join(
    ["index 111..222 100644", "--- a/moss/alpha.py", "+++ b/moss/alpha.py", "@@ -1,40 +1,44 @@"]
    + [f"+added line {index}" for index in range(200)]
    + [f"-removed line {index}" for index in range(40)]
    + ["diff --git a/moss/beta.py b/moss/beta.py", "@@ -10,3 +10,5 @@", "+beta change"]
)

SEARCH_OUTPUT = "\n".join(
    [f"moss/alpha.py:{index}:    call_site({index})" for index in range(1, 40)]
    + [f"moss/beta.py:{index}:    call_site({index})" for index in range(1, 12)]
)

LIST_OUTPUT = "\n".join(
    ["[D] moss", "[D] tests"]
    + [f"[F] moss/module_{index}.py" for index in range(80)]
    + [f"[F] docs/page_{index}.md" for index in range(15)]
)


def test_detect_kind_reads_the_shape_not_just_the_tool_name():
    assert detect_kind("run_shell", {"command": "pytest -q"}, PYTEST_OUTPUT) == "pytest"
    assert detect_kind("run_shell", {"command": "ruff check ."}, RUFF_OUTPUT) == "lint"
    assert detect_kind("run_shell", {"command": "git diff"}, GIT_DIFF_OUTPUT) == "git_diff"
    assert detect_kind("search_text", {"pattern": "call_site"}, SEARCH_OUTPUT) == "search_text"
    assert detect_kind("list_files", {"path": "."}, LIST_OUTPUT) == "list_files"
    assert detect_kind("read_file", {"path": "a.txt"}, "alpha\nbeta\n") == "generic"


def test_pytest_compression_keeps_failed_cases_and_assert_lines():
    compressed, stats = compress("pytest", PYTEST_OUTPUT, 2000)

    assert "test_parses_nested_payload" in compressed
    assert "test_retries_on_timeout" in compressed
    assert "assert {'ok': False} == {'ok': True}" in compressed
    assert "assert 1 == 3" in compressed
    assert "2 failed, 410 passed" in compressed
    # 中间那 60 行无信息的调用栈正是要被丢掉的部分。
    assert "step_5()" not in compressed
    assert len(compressed) <= 2000
    assert set(stats["failed_cases"]) == {"test_parses_nested_payload", "test_retries_on_timeout"}
    assert stats["error_signal_lost"] is False


def test_lint_compression_aggregates_by_code():
    compressed, stats = compress("lint", RUFF_OUTPUT, 1200)

    assert "E501: 120 occurrence(s)" in compressed
    assert "F401: 30 occurrence(s)" in compressed
    assert "and 117 more E501" in compressed
    assert stats["codes"] == {"E501": 120, "F401": 30}
    assert stats["total_findings"] == 150
    assert len(compressed) <= 1200


def test_git_diff_compression_keeps_hunk_headers_and_counts():
    compressed, stats = compress("git_diff", GIT_DIFF_OUTPUT, 600)

    assert "diff --git a/moss/alpha.py" in compressed
    assert "@@ -1,40 +1,44 @@" in compressed
    assert "+200/-40" in compressed
    assert stats["files"]["moss/beta.py"] == {"added": 1, "removed": 0}
    assert len(compressed) <= 600


def test_git_diff_small_enough_is_kept_verbatim():
    small = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new"

    compressed, _ = compress("git_diff", small, 4000)

    assert compressed == small


def test_search_compression_caps_hits_per_file_and_counts_the_rest():
    compressed, stats = compress("search_text", SEARCH_OUTPUT, 1000)

    assert compressed.count("moss/alpha.py:") <= 4
    assert "36 more match(es) in moss/alpha.py" in compressed
    assert "matches: 50 across 2 file(s)" in compressed
    assert stats["files"]["moss/beta.py"] == 11


def test_list_files_compression_aggregates_by_extension():
    compressed, stats = compress("list_files", LIST_OUTPUT, 800)

    assert "[D] moss" in compressed
    assert "80x .py" in compressed
    assert "15x .md" in compressed
    assert stats["files"] == 95
    assert stats["directories"] == 2


def test_generic_compression_is_the_fallback():
    text = "\n".join(f"row {index}" for index in range(2000))

    compressed, stats = compress("generic", text, 500)

    assert len(compressed) <= 500
    assert compressed.startswith("row 0")
    assert stats["compressor_kind"] == "generic"


def test_exit_code_always_survives_compression():
    for kind, text in (
        ("pytest", PYTEST_OUTPUT),
        ("lint", RUFF_OUTPUT),
        ("git_diff", GIT_DIFF_OUTPUT),
        ("generic", "exit_code: 137\nstdout:\n" + "x" * 50000),
    ):
        compressed, _ = compress(kind, text, 300)
        assert compressed.splitlines()[0].startswith("exit_code:"), kind


def test_error_signal_lost_is_reported_when_every_failure_line_is_dropped():
    text = "exit_code: 1\nstdout:\n" + "\n".join(f"quiet line {index}" for index in range(500))
    text += "\nTraceback (most recent call last):\nValueError: boom"

    compressed, stats = compress("generic", text, 300)

    assert stats["error_signal_lost"] is True
    assert "ValueError" not in compressed


def test_registry_is_extensible():
    try:
        register("custom_kind", lambda text, budget: ("compressed", {"custom": True}))
        assert "custom_kind" in registered_kinds()
        compressed, stats = compress("custom_kind", "anything", 100)
        assert compressed == "compressed"
        assert stats["custom"] is True
    finally:
        output_compressors._REGISTRY.pop("custom_kind", None)


def test_unknown_kind_falls_back_to_generic():
    compressed, stats = compress("no-such-kind", "alpha\nbeta", 200)

    assert compressed == "alpha\nbeta"
    assert stats["compressor_kind"] == "no-such-kind"


def _agent_with_active_run(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )
    task_state = TaskState.create(task_id="task_test", user_request="x", run_id="run_test")
    agent.current_task_state = task_state
    agent.current_run_dir = agent.start_run(task_state)
    return agent


def test_offloaded_shell_output_keeps_the_never_lose_list(tmp_path):
    agent = _agent_with_active_run(tmp_path)
    noisy = PYTEST_OUTPUT + "\n" + "\n".join(f"noise line {index}" for index in range(3000))

    content, metadata = prepare_tool_output(agent, "run_shell", {"command": "pytest -q"}, noisy)

    # 不可丢失清单：exit_code、artifact 指针、失败原因。
    assert content.splitlines()[0] == "exit_code: 1"
    assert 'read_artifact("artifacts/' in content
    assert "2 failed, 410 passed" in content
    assert "assert 1 == 3" in content
    assert metadata["truncated_bytes_lost"] == 0
    assert metadata["compressor_kind"] == "pytest"
    assert metadata["error_signal_lost"] is False


def test_execution_metadata_still_carries_status_and_affected_paths(tmp_path):
    agent = _agent_with_active_run(tmp_path)
    (tmp_path / "big.py").write_text("\n".join(f"def fn_{index}(): pass" for index in range(1200)), encoding="utf-8")

    result = agent.execute_tool("read_file", {"path": "big.py", "start": 1, "end": 1200})

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["affected_paths"] == []
    assert result.metadata["artifact_path"].startswith("artifacts/")
    assert result.metadata["truncated_bytes_lost"] == 0
