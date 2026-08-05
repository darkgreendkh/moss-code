"""审批与写入之间的 TOCTOU（spec-03 §4.3）。

审批展示的是**当时**的 diff，执行发生在之后。中间文件被换掉的话，
用户批的和实际执行的就不是一回事了。
"""

import pytest

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.tools import NOFOLLOW_SUPPORTED


def _build_agent(tmp_path, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    kwargs.setdefault("approval_policy", "ask")
    return Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        **kwargs,
    )


def test_content_changed_after_approval_is_refused(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("original\n", encoding="utf-8")
    agent = _build_agent(tmp_path)

    def approve_then_swap(name, args):
        # 模拟审批期间另一个进程改写了目标文件。
        target.write_text("someone else wrote this\n", encoding="utf-8")
        return True

    agent.approve = approve_then_swap
    result = agent.run_tool("write_file", {"path": "mod.py", "content": "agent wrote this\n"})

    assert "preconditions changed after approval" in result
    assert agent._last_tool_result_metadata["tool_error_code"] == "precondition_failed"
    assert target.read_text(encoding="utf-8") == "someone else wrote this\n"


def test_file_created_after_approval_is_refused(tmp_path):
    """审批时文件不存在（新建），执行时已经有了——那份内容用户没看过。"""
    agent = _build_agent(tmp_path)

    def approve_then_create(name, args):
        (tmp_path / "new.py").write_text("sneaked in\n", encoding="utf-8")
        return True

    agent.approve = approve_then_create
    result = agent.run_tool("write_file", {"path": "new.py", "content": "agent content\n"})

    assert "preconditions changed after approval" in result
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "sneaked in\n"


@pytest.mark.skipif(not NOFOLLOW_SUPPORTED, reason="symlinks require POSIX-ish support")
def test_target_swapped_for_a_symlink_after_approval_is_refused(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("original\n", encoding="utf-8")
    inside = tmp_path / "other.py"
    inside.write_text("other\n", encoding="utf-8")
    agent = _build_agent(tmp_path)

    def approve_then_symlink(name, args):
        target.unlink()
        target.symlink_to(inside)
        return True

    agent.approve = approve_then_symlink
    result = agent.run_tool("write_file", {"path": "mod.py", "content": "agent wrote this\n"})

    assert "preconditions changed after approval" in result
    assert inside.read_text(encoding="utf-8") == "other\n"


def test_an_unchanged_target_still_writes(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("original\n", encoding="utf-8")
    agent = _build_agent(tmp_path, approval_policy="auto")

    result = agent.run_tool("write_file", {"path": "mod.py", "content": "agent wrote this\n"})

    assert result.startswith("wrote")
    assert target.read_text(encoding="utf-8") == "agent wrote this\n"


def test_edit_file_is_covered_too(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("keep\nold\n", encoding="utf-8")
    agent = _build_agent(tmp_path)

    def approve_then_swap(name, args):
        target.write_text("keep\nold\nextra\n", encoding="utf-8")
        return True

    agent.approve = approve_then_swap
    result = agent.run_tool("edit_file", {"path": "mod.py", "old_text": "old", "new_text": "new"})

    assert "preconditions changed after approval" in result


@pytest.mark.skipif(not NOFOLLOW_SUPPORTED, reason="symlinks require POSIX-ish support")
def test_a_symlink_pointing_outside_the_workspace_is_rejected(tmp_path):
    """指向仓库外的软链由路径锚定挡下，写入永远落不到外面。"""
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(outside)
    agent = _build_agent(tmp_path, approval_policy="auto")

    result = agent.run_tool("write_file", {"path": "link.py", "content": "x\n"})

    assert "path escapes workspace" in result
    assert outside.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.skipif(not NOFOLLOW_SUPPORTED, reason="symlinks require POSIX-ish support")
def test_a_symlink_inside_the_workspace_writes_to_its_resolved_target(tmp_path):
    """指向仓库内的软链是正常用法：路径锚定 resolve 之后写的是真实文件。"""
    inside = tmp_path / "real.py"
    inside.write_text("real\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(inside)
    agent = _build_agent(tmp_path, approval_policy="auto")

    result = agent.run_tool("write_file", {"path": "link.py", "content": "x\n"})

    assert result.startswith("wrote real.py")
    assert inside.read_text(encoding="utf-8") == "x\n"
