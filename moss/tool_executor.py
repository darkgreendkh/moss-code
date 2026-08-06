"""Structured tool execution for the agent runtime."""

import difflib
import hashlib
import json
import re
from dataclasses import dataclass

from . import output_compressors
from .clock import now
from .injection import scan as scan_for_injection
from .token_budget import MAX_TOOL_OUTPUT, clip
from .tools import ToolRunOutput, classify_shell_command
from .workspace import invalidate_git_facts_cache


# 超过这个字符数的工具输出就落盘，prompt 里只放摘要 + 指针。
# 阈值刻意偏低：一份 4000 字符的输出进 prompt 已经是上千 token，
# 而它里面通常只有几行是决策需要的。
ARTIFACT_THRESHOLD = 4000
# 卸载后进 prompt 的摘要上限（字符）。
ARTIFACT_PREVIEW_CHARS = 2000
# 压缩统计里允许进 metadata/trace 的字段（其余是明细表，只留给调用方）。
COMPRESSION_METADATA_KEYS = (
    "compressor_kind",
    "compressed_chars",
    "original_chars",
    "error_signal_lost",
)


# 工具输出超上限时，保留哪一端取决于关键信息的位置。
# run_shell 的输出是 "exit_code: ... / stdout / stderr"，退出码在顶部、
# 报错在底部，所以两端都要留（middle）；读文件/列目录/搜索的信息在开头，
# 保头即可。默认保头。
_TRUNCATION_KEEP = {
    "run_shell": "middle",
}

# 会“碰到某个目录”的文件类工具。碰到之后才去找那个目录的就近指令文档。
_FILE_TOOLS = ("read_file", "write_file", "edit_file")

# shell 分级 -> 审批用的粗粒度风险。denied 不会走到这里（更早就被拒了），
# 但仍然映射一份，免得将来漏一个分支就静默降级成 low。
_SHELL_RISK_LEVELS = {
    "read_only": "low",
    "test": "low",
    "write": "medium",
    "network": "high",
    "high": "high",
    "denied": "high",
}


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


@dataclass(frozen=True)
class ApprovalReceipt:
    """用户批准的到底是"哪一次改动"。

    为什么需要它：审批展示的是**当时**的 diff，而执行发生在之后。中间文件被换掉
    （被另一个进程改写、被换成软链）的话，用户批的和实际执行的就不是一回事了。
    回执把审批那一刻的文件内容指纹记下来，执行前再核一次对不对得上。
    """

    tool: str
    resolved_paths: tuple = ()
    expected_sha256: dict = None      # path -> sha256；None 表示当时文件不存在
    diff_digest: str = ""
    approved_at: str = ""
    scope: str = "once"


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
    risk = classify_shell_command((args or {}).get("command", ""))
    risk_class = risk.level
    return {
        "shell_risk_class": risk_class,
        "shell_risk_reasons": list(risk.reasons),
        "shell_undecidable": risk.undecidable,
        "risk_level": _SHELL_RISK_LEVELS.get(risk_class, "medium"),
        "read_only": False,
    }


def _scan_result_for_injection(agent, name, args, content):
    """扫工具输出里的注入痕迹。工具输出是数据，里面的"指令"不该被执行。"""
    if not getattr(agent, "injection_scan", True):
        return None
    source = f"{name}:{str((args or {}).get('path', '') or (args or {}).get('command', ''))[:60]}"
    return scan_for_injection(content, source=source)


def _file_sha256(path):
    """文件内容指纹；文件不存在返回 None（和"空文件"是两回事）。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def _build_receipt(agent, name, args):
    """在**展示审批之前**拍下目标文件的状态。"""
    if name not in {"write_file", "edit_file"}:
        return None
    raw_path = str((args or {}).get("path", "")).strip()
    if not raw_path:
        return None
    try:
        path = agent.path(raw_path)
        rel_path = path.relative_to(agent.root).as_posix()
    except Exception:
        return None
    preview = file_change_preview(agent, name, args)
    return ApprovalReceipt(
        tool=name,
        resolved_paths=(rel_path,),
        expected_sha256={rel_path: _file_sha256(path)},
        diff_digest=hashlib.sha256(preview.encode("utf-8")).hexdigest(),
        approved_at=now(),
    )


def _stale_preconditions(agent, receipt):
    """执行前重新核对回执。返回不一致的说明，一致则返回空串。"""
    if receipt is None:
        return ""
    for rel_path, expected in (receipt.expected_sha256 or {}).items():
        path = agent.root / rel_path
        if path.is_symlink():
            # 审批之后目标被换成软链：写入会落到软链指向的地方。
            return f"{rel_path} is now a symlink"
        if _file_sha256(path) != expected:
            return f"{rel_path} changed on disk"
    return ""


def _policy_decision(agent, tool, name, args):
    """按能力标签和路径作用域判定。没有策略对象时返回 None（行为与以前一致）。"""
    policy = getattr(agent, "policy", None)
    spec = tool.get("spec")
    if policy is None or spec is None:
        return None
    return policy.decide(
        spec,
        args,
        resolved_paths=_resolved_relative_paths(agent, name, args, path_scope=spec.path_scope),
    )


def _resolved_relative_paths(agent, name, args, path_scope="workspace"):
    """把工具参数里的路径解析成仓库内相对路径。

    解析失败（逃逸、不存在）不在这里报错——路径锚定是 Moss.path() / Moss.run_path()
    的职责，策略层只回答"允不允许碰这些路径"。作用域要按 ToolSpec 走：run_dir 的
    工具用工作区根去解析会得到一个根本不存在的路径，策略判定就跟着错位。
    """
    raw = str((args or {}).get("path", "")).strip()
    if not raw:
        return ()
    resolver = agent.run_path if path_scope == "run_dir" else agent.path
    try:
        return (resolver(raw).relative_to(agent.root).as_posix(),)
    except Exception:
        return (raw.replace("\\", "/").lstrip("./"),)


def _denied_shell_reason(name, args):
    """命中 deny 清单时返回原因，否则返回空串。"""
    if name != "run_shell":
        return ""
    risk = classify_shell_command((args or {}).get("command", ""))
    if risk.level != "denied":
        return ""
    return "; ".join(risk.reasons) or "command is on the deny list"


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
    return {
        "shell_risk_class": shell_metadata["shell_risk_class"],
        "shell_risk_reasons": shell_metadata["shell_risk_reasons"],
        "shell_undecidable": shell_metadata["shell_undecidable"],
    }


def prepare_tool_output(agent, name, args, raw_text):
    """把工具原始输出变成"进 prompt 的内容 + 相应 metadata"。

    为什么要这一层：硬截断是**有损**的——砍掉的 12000 行既不进摘要也不落盘，
    模型无从取回，而失败原因偏偏常常在那里面。这里改成：完整输出落进 run 目录，
    prompt 里放摘要和一个可以用 read_artifact 取回的指针。
    """
    raw_text = str(raw_text)
    keep = _TRUNCATION_KEEP.get(name, "head")
    # read_artifact 自己的输出不再卸载：模型刚按行区间取回的内容又被换成指针，
    # 只会让它绕圈。
    if name == "read_artifact" or len(raw_text) <= ARTIFACT_THRESHOLD:
        content = clip(raw_text, keep=keep)
        return content, {"truncated_bytes_lost": max(0, len(raw_text) - MAX_TOOL_OUTPUT)}

    # 落盘的文本先过脱敏边界：artifact 和 trace/report 一样是长期留在磁盘上的工件。
    safe_text = agent.redact_text(raw_text)
    stored = agent.store_tool_artifact(name, safe_text)
    if stored is None:
        content = clip(raw_text, keep=keep)
        return content, {"truncated_bytes_lost": max(0, len(raw_text) - MAX_TOOL_OUTPUT)}

    artifact_path, lines = stored
    summary, stats = compress_tool_output(name, args, safe_text, ARTIFACT_PREVIEW_CHARS)
    # 压缩器的完整 stats 里有按文件/按 CODE 的明细，可能上百条。
    # 进 metadata 的只取标量：trace 是时间线，不该被一份统计表撑爆。
    compression = {key: stats[key] for key in COMPRESSION_METADATA_KEYS if key in stats}
    pointer = (
        f'... full output is {lines} lines; '
        f'read it with read_artifact("{artifact_path}", start, end)'
    )
    return (
        f"{summary}\n{pointer}",
        {
            "artifact_path": artifact_path,
            "artifact_lines": lines,
            "truncated_bytes_lost": 0,
            **compression,
        },
    )


def compress_tool_output(name, args, text, budget_chars):
    """把大输出压成进 prompt 的摘要，按输出类型选压缩器。"""
    kind = output_compressors.detect_kind(name, args, text)
    return output_compressors.compress(kind, text, budget_chars)


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
        risk = classify_shell_command(command)
        # 审批摘要要说清"为什么算这个等级"，否则用户只能盲批。
        reasons = "; ".join(risk.reasons)
        suffix = f" — {reasons}" if reasons else ""
        return f"[{risk.level}] {command_summary}{suffix}"
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

    def execute(self, name, args, defer_side_effects=False):
        """执行一次工具调用。

        `defer_side_effects=True` 时跳过 memory 写入和 process note——
        并发批执行会在多个线程里同时调这个函数，而这两处都在写共享状态。
        调用方负责回到主线程后按 Action.index 顺序补做，
        这样并发也不会改变记录顺序。
        """
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

        decision = _policy_decision(agent, tool, name, args)
        if decision is not None and not decision.allowed:
            return ToolExecutionResult(
                content=f"error: policy refused {name}: {decision.reason}",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="capability_denied",
                    security_event_type="capability_denied",
                    risk_level=_risk_level_for(tool, name, args),
                    read_only=_read_only_for(tool, name, args),
                    extra=_extra_metadata_for(name, args),
                ),
            )

        denied_reason = _denied_shell_reason(name, args)
        if denied_reason:
            # deny 清单是"连审批机会都不给"的一档：这些命令没有任何正当用法
            # 值得用一次误点来换（`rm -rf /`、下载内容直接管道进解释器、fork bomb）。
            return ToolExecutionResult(
                content=f"error: refused to run {name}: {denied_reason}",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="command_denied",
                    security_event_type="denied_command",
                    risk_level="high",
                    read_only=False,
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

        receipt = _build_receipt(agent, name, args) if tool["risky"] else None
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

        stale = _stale_preconditions(agent, receipt)
        if stale:
            # 审批之后、执行之前文件被换掉了。这正是 TOCTOU 的形状：
            # 用户批的是当时那份 diff，不是现在这份内容。
            return ToolExecutionResult(
                content=f"error: {name} preconditions changed after approval: {stale}; re-request approval",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="precondition_failed",
                    security_event_type="precondition_failed",
                    risk_level=_risk_level_for(tool, name, args),
                    read_only=_read_only_for(tool, name, args),
                    extra=_extra_metadata_for(name, args),
                ),
            )

        before_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else {}
        after_snapshot = before_snapshot
        try:
            output = _normalize_tool_output(tool["run"](args))
            content, output_metadata = prepare_tool_output(agent, name, args, output.content)
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
            extra_metadata.update(output_metadata)
            lost = int(output_metadata.get("truncated_bytes_lost", 0))
            if lost:
                agent.truncated_bytes_lost = getattr(agent, "truncated_bytes_lost", 0) + lost
            if output_metadata.get("error_signal_lost"):
                agent.error_signal_lost_count = getattr(agent, "error_signal_lost_count", 0) + 1
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
            finding = _scan_result_for_injection(agent, name, args, content)
            if finding is not None:
                # 只收紧策略，不拒绝执行：误报是必然的（正常代码里就有
                # "ignore previous" 这样的字符串），把误报变成"任务直接失败"
                # 比漏报还难受。所以后果是"接下来的 risky 工具一律走审批"。
                agent.flag_injection_suspected(finding)
                extra_metadata.update(
                    {
                        "security_event_type": "prompt_injection_suspected",
                        "injection_pattern": finding.pattern,
                        "injection_excerpt": agent.redact_text(finding.excerpt),
                    }
                )
            if not defer_side_effects:
                agent.update_memory_after_tool(name, args, content)
            if tool_status in {"ok", "partial_success"} and name in _FILE_TOOLS:
                # 模型第一次碰到某个子目录时，才把那个目录的就近 AGENTS.md 注进来。
                agent.note_nearby_instructions(args.get("path", ""))
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
            if not defer_side_effects:
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
