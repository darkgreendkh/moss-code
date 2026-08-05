"""Structured tool execution for the agent runtime."""

import difflib
import json
import re
from dataclasses import dataclass

from .token_budget import clip
from .tools import ToolRunOutput, classify_shell_command
from .workspace import invalidate_git_facts_cache


# 工具输出超上限时，保留哪一端取决于关键信息的位置。
# run_shell 的输出是 "exit_code: ... / stdout / stderr"，退出码在顶部、
# 报错在底部，所以两端都要留（middle）；读文件/列目录/搜索的信息在开头，
# 保头即可。默认保头。
_TRUNCATION_KEEP = {
    "run_shell": "middle",
}

_SHELL_RISK_LEVELS = {
    "read_only": "low",
    "test": "low",
    "general": "medium",
    "destructive_or_network": "high",
}


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


def _metadata(
    tool_status,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    read_only=True,
    affected_paths=None,
    workspace_changed=False,
    workspace_fingerprint="",
    diff_summary=None,
    extra=None,
):
    result = {
        "tool_status": tool_status,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level,
        "read_only": read_only,
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
    }
    if workspace_fingerprint:
        result["workspace_fingerprint"] = workspace_fingerprint
    if extra:
        result.update(dict(extra))
    return result


def _normalize_tool_output(value):
    if isinstance(value, ToolRunOutput):
        return value
    return ToolRunOutput(content=str(value))


def _shell_risk_metadata(name, args):
    if name != "run_shell":
        return None
    risk_class = classify_shell_command((args or {}).get("command", ""))
    return {
        "shell_risk_class": risk_class,
        "risk_level": _SHELL_RISK_LEVELS.get(risk_class, "medium"),
        "read_only": False,
    }


def _risk_level_for(tool, name, args):
    shell_metadata = _shell_risk_metadata(name, args)
    if shell_metadata:
        return shell_metadata["risk_level"]
    return "high" if tool["risky"] else "low"


def _read_only_for(tool, name, args):
    shell_metadata = _shell_risk_metadata(name, args)
    if shell_metadata:
        return shell_metadata["read_only"]
    return not tool["risky"]


def _extra_metadata_for(name, args):
    shell_metadata = _shell_risk_metadata(name, args)
    if not shell_metadata:
        return {}
    return {"shell_risk_class": shell_metadata["shell_risk_class"]}


def approval_summary(agent, name, args):
    # 审批提示要一眼能看懂：写文件类工具展示脱敏 diff，shell 展示风险分级 + 命令摘要。
    args = args or {}
    if name in {"write_file", "edit_file"}:
        preview = file_change_preview(agent, name, args)
        if preview:
            return preview
    if name == "run_shell":
        command = str(args.get("command", ""))
        command_summary = command if len(command) <= 200 else command[:197] + "..."
        risk_class = classify_shell_command(command)
        return f"[{risk_class}] {command_summary}"
    summary = json.dumps(args, ensure_ascii=True)
    return summary if len(summary) <= 200 else summary[:197] + "..."


def file_change_preview(agent, name, args):
    """为 write_file/edit_file 生成脱敏后的 unified diff 预览，最长 800 字符。"""
    raw_path = str(args.get("path", "")).strip()
    if not raw_path:
        return ""
    try:
        path = agent.path(raw_path)
    except Exception:
        return raw_path
    try:
        before = path.read_text(encoding="utf-8") if path.exists() and path.is_file() else ""
    except OSError:
        before = ""
    if name == "write_file":
        after = str(args.get("content", ""))
    else:
        old_text = str(args.get("old_text", ""))
        new_text = str(args.get("new_text", ""))
        after = before.replace(old_text, new_text, 1) if old_text and before.count(old_text) == 1 else before
    try:
        rel_path = path.relative_to(agent.root).as_posix()
    except ValueError:
        rel_path = raw_path
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )
    if not diff_lines:
        return rel_path
    return agent.redact_text(clip("\n".join([rel_path, *diff_lines]), 800))


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent

    def execute(self, name, args):
        agent = self.agent
        if agent.allowed_tools is not None and name not in agent.allowed_tools:
            return ToolExecutionResult(
                content=f"error: tool '{name}' is not allowed in this run",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="tool_not_allowed",
                    risk_level="high",
                    read_only=False,
                ),
            )

        tool = agent.tools.get(name)
        if tool is None:
            return ToolExecutionResult(
                content=f"error: unknown tool '{name}'",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="unknown_tool",
                    risk_level="high",
                    read_only=False,
                ),
            )

        try:
            agent.validate_tool(name, args)
        except Exception as exc:
            example = agent.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            return ToolExecutionResult(
                content=message,
                metadata=_metadata(
                    "rejected",
                    tool_error_code="invalid_arguments",
                    security_event_type=security_event_type,
                    risk_level=_risk_level_for(tool, name, args),
                    read_only=_read_only_for(tool, name, args),
                    extra=_extra_metadata_for(name, args),
                ),
            )

        if agent.repeated_tool_call(name, args):
            return ToolExecutionResult(
                content=f"error: repeated identical tool call for {name}; choose a different tool or return a final answer",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="repeated_identical_call",
                    risk_level=_risk_level_for(tool, name, args),
                    read_only=_read_only_for(tool, name, args),
                    extra=_extra_metadata_for(name, args),
                ),
            )

        if tool["risky"] and not agent.approve(name, args):
            return ToolExecutionResult(
                content=f"error: approval denied for {name}",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="approval_denied",
                    security_event_type="read_only_block" if agent.read_only else "approval_denied",
                    risk_level=_risk_level_for(tool, name, args),
                    read_only=_read_only_for(tool, name, args),
                    extra=_extra_metadata_for(name, args),
                ),
            )

        before_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else {}
        after_snapshot = before_snapshot
        try:
            output = _normalize_tool_output(tool["run"](args))
            content = clip(output.content, keep=_TRUNCATION_KEEP.get(name, "head"))
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            if tool["risky"]:
                # git 事实带 500ms TTL 缓存，risky 工具刚改完工作区就必须让它失效，
                # 否则下一轮 prefix 里的 status 还是执行前的样子。
                invalidate_git_facts_cache()
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            tool_status = "ok"
            tool_error_code = ""
            extra_metadata = _extra_metadata_for(name, args)
            if name == "run_shell":
                exit_code = output.exit_code
                if exit_code is None:
                    match = re.search(r"exit_code:\s*(-?\d+)", content)
                    exit_code = int(match.group(1)) if match else 0
                extra_metadata.update(
                    {
                        "exit_code": exit_code,
                        "stdout_chars": len(output.stdout),
                        "stderr_chars": len(output.stderr),
                    }
                )
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            agent.update_memory_after_tool(name, args, content)
            metadata = _metadata(
                tool_status,
                tool_error_code=tool_error_code,
                risk_level=_risk_level_for(tool, name, args),
                read_only=_read_only_for(tool, name, args),
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
                extra=extra_metadata,
            )
            agent.record_process_note_for_tool(name, metadata)
            return ToolExecutionResult(content=content, metadata=metadata)
        except Exception as exc:
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            if tool["risky"]:
                # git 事实带 500ms TTL 缓存，risky 工具刚改完工作区就必须让它失效，
                # 否则下一轮 prefix 里的 status 还是执行前的样子。
                invalidate_git_facts_cache()
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            metadata = _metadata(
                "partial_success" if workspace_changed else "error",
                tool_error_code="tool_partial_success" if workspace_changed else "tool_failed",
                security_event_type=security_event_type,
                risk_level=_risk_level_for(tool, name, args),
                read_only=_read_only_for(tool, name, args),
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
                extra=_extra_metadata_for(name, args),
            )
            agent.record_process_note_for_tool(name, metadata)
            return ToolExecutionResult(content=f"error: tool {name} failed: {exc}", metadata=metadata)
