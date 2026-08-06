"""Observable contracts kept while Moss becomes a composition facade."""

import ast
from pathlib import Path

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.agent.state import TaskState


def _agent(tmp_path):
    return Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        sandbox="off",
    )


def test_context_facade_keeps_history_rendering_contract(tmp_path):
    agent = _agent(tmp_path)
    agent.record({"role": "user", "content": "inspect the workspace"})

    assert "inspect the workspace" in agent.history_text()


def test_execution_facade_keeps_tool_result_contract(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = _agent(tmp_path)

    assert "README.md" in agent.run_tool("list_files", {"path": "."})


def test_run_facade_keeps_report_contract(tmp_path):
    agent = _agent(tmp_path)
    state = TaskState.create(
        task_id="task_contract", run_id="run_contract", user_request="x"
    )

    report = agent.build_report(state)

    assert report["task_id"] == "task_contract"
    assert report["run_id"] == "run_contract"


def test_extension_facade_keeps_default_skill_scope_contract(tmp_path):
    agent = _agent(tmp_path)

    assert agent.skill_scope_hint() == ""


def test_runtime_methods_are_thin_component_delegates():
    tree = ast.parse(Path("moss/runtime.py").read_text(encoding="utf-8"))
    moss_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Moss"
    )
    local_methods = {
        "__init__",
        "from_session",
        "_ensure_session_shape",
        "__getattr__",
        "remember",
        "_normalize_allowed_tools",
        "looks_sensitive_env_name",
        "_memory_rejection",
        "new_task_id",
        "new_run_id",
    }
    services = {
        "context_service",
        "execution_service",
        "run_coordinator",
        "extension_manager",
    }

    for method in (
        node for node in moss_class.body if isinstance(node, ast.FunctionDef)
    ):
        if method.name in local_methods:
            continue
        assert len(method.body) == 1 and isinstance(method.body[0], ast.Return), (
            method.name
        )
        value = method.body[0].value
        target = value.func if isinstance(value, ast.Call) else value
        assert isinstance(target, ast.Attribute), method.name
        assert isinstance(target.value, ast.Attribute), method.name
        assert (
            isinstance(target.value.value, ast.Name) and target.value.value.id == "self"
        ), method.name
        assert target.value.attr in services, method.name
