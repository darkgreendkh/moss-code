"""Structured tool execution for the agent runtime."""

import difflib
import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass

from ..extensions import hooks as hookslib
from ..clock import now
from ..context import compressors as output_compressors
from ..context.repository.workspace import invalidate_git_facts_cache
from ..context.token_budget import MAX_TOOL_OUTPUT, clip
from ..runs import ledger as action_ledger
from ..runs.observability import events as trace_events
from .registry import ToolRunOutput, classify_shell_command
from .safety.injection import scan as scan_for_injection


# 超过这个字符数的工具输出就落盘，prompt 里只放摘要 + 指针。
# 阈值**就是硬截断的上限**：卸载存在的理由是"该被砍掉的部分要能取回"，
# 而不是"能省则省"。定得比上限低（历史上是 4000）会让一份本来装得下的输出
# 也被换成摘要 + 指针，模型为了看自己刚读到的东西必须再花一步 read_artifact，
# 一次 read_file 变成两步且仍然只拿到一部分——25 步烧完还没答案就是这么来的。
ARTIFACT_THRESHOLD = MAX_TOOL_OUTPUT
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


def _git_stash_object(agent):
    """`git stash create` 出来的对象 id。不是 git 仓库或失败时返回空串。

    它不改工作区、也不进 stash 栈，只是把当前改动固化成一个可以找回来的对象。
    run_shell 改了什么要执行完才知道，逐文件备份来不及——这是它唯一的兜底。
    """
    try:
        result = subprocess.run(
            ["git", "stash", "create"],
            cwd=str(agent.root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


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
        # run 级 allowlist 叠加当前 skill 的 allowed-tools 临时覆盖（spec-09 §9.4）。
        allowed_tools = agent.effective_allowed_tools()
        if allowed_tools is not None and name not in allowed_tools:
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

        # pre_tool 是唯一能影响控制流的钩子。放在审批之前：被钩子拒掉的调用
        # 不该先去打扰用户按一次 y。
        hook = agent.fire_hook(hookslib.PRE_TOOL, {"tool": name, "args": args, "risky": tool["risky"]})
        if hook.denied:
            return ToolExecutionResult(
                content=f"error: {name} was denied by the pre_tool hook: {hook.to_dict()['reason'] or 'exit code 2'}",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="hook_denied",
                    security_event_type="hook_denied",
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
        # 意图先于副作用落盘：崩在"文件写完但记录没写"的窗口里时，这条 intent
        # 就是唯一能说明"当时准备做什么"的证据（spec-07 §4.6）。
        intent = self._emit_intent(name, args, tool, receipt) if tool["risky"] else None
        started_at = time.monotonic()
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
            self._emit_receipt(intent, metadata, started_at)
            # post_tool 纯观察：它的退出码不改变任何东西（"写完必跑 ruff"这类
            # 用法要的是副作用，不是否决权）。
            agent.fire_hook(
                hookslib.POST_TOOL,
                {
                    "tool": name,
                    "args": args,
                    "status": tool_status,
                    "affected_paths": affected_paths,
                },
            )
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
            # 失败也要有回执：没有它，"跑了但报错"和"根本没跑"在恢复时长得一样。
            self._emit_receipt(intent, metadata, started_at)
            return ToolExecutionResult(content=f"error: tool {name} failed: {exc}", metadata=metadata)

    def _emit_intent(self, name, args, tool, receipt):
        """执行前落一条 action_intent。任何异常都吞掉——记账绝不能挡住执行。"""
        agent = self.agent
        task_state = getattr(agent, "current_task_state", None)
        if task_state is None:
            return None
        try:
            spec = tool.get("spec")
            intent = action_ledger.build_intent(
                name=name,
                args=args,
                capabilities=getattr(spec, "capabilities", ()) or (),
                risk=_risk_level_for(tool, name, args),
                expected_sha=dict((receipt.expected_sha256 or {}) if receipt else {}),
                approval_receipt_id=receipt.diff_digest if receipt else "",
                previous_receipt_id=getattr(agent, "last_action_receipt_id", ""),
                resolve_path=agent.path,
            )
            agent.emit_trace(task_state, trace_events.ACTION_INTENT, dict(intent))
            self._capture_undo(task_state, intent)
            return intent
        except Exception:
            return None

    def _capture_undo(self, task_state, intent):
        """把将被改写文件的旧内容存进 run 目录，供 `/rewind` 回滚。

        只对写类工具存内容：run_shell 改了什么要执行完才知道，那时旧内容已经没了。
        有 git 时顺手记一个 `git stash create` 对象——回滚不了也至少能告诉用户
        去哪里找回来，而不是假装这一步可以撤销。
        """
        agent = self.agent
        run_id = task_state.run_id
        paths = sorted(dict(intent.get("expected_sha", {}) or {}))
        files = {}
        restorable = bool(paths)
        for rel_path in paths:
            target = agent.root / rel_path
            try:
                files[rel_path] = target.read_text(encoding="utf-8") if target.is_file() else None
            except (OSError, UnicodeDecodeError):
                # 二进制或读不动：老实说这一步回滚不了，别写一份读错的内容回去。
                files[rel_path] = None
                restorable = False
        manifest = {
            "action_id": intent["action_id"],
            "run_id": run_id,
            "tool": intent["tool"],
            "created_at": now(),
            "paths": paths,
            "existed": {rel_path: files.get(rel_path) is not None for rel_path in paths},
            "sha_before": dict(intent.get("expected_sha", {}) or {}),
            "sha_after": {},
            "restorable": restorable,
            "git_object": _git_stash_object(agent) if not restorable else "",
            "checkpoint_id": str((agent.current_checkpoint() or {}).get("checkpoint_id", "")),
            "history_length": len(agent.session.get("history", []) or []),
        }
        agent.run_store.write_undo(run_id, intent["action_id"], manifest, files)
        agent.session.setdefault("undo", []).append(
            {
                "run_id": run_id,
                "action_id": intent["action_id"],
                "tool": intent["tool"],
                "restorable": restorable,
                "history_length": manifest["history_length"],
                "checkpoint_id": manifest["checkpoint_id"],
            }
        )
        return manifest

    def _emit_receipt(self, intent, metadata, started_at):
        agent = self.agent
        task_state = getattr(agent, "current_task_state", None)
        if intent is None or task_state is None:
            return None
        try:
            affected = list(metadata.get("affected_paths", []) or [])
            after_sha = {}
            for rel_path in affected + list(intent.get("expected_sha", {}) or {}):
                after_sha[rel_path] = action_ledger.file_sha(agent.root / rel_path)
            payload = action_ledger.build_receipt(
                intent,
                status=str(metadata.get("tool_status", "")),
                exit_code=metadata.get("exit_code"),
                affected_paths=affected,
                after_sha=after_sha,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            agent.emit_trace(task_state, trace_events.ACTION_RECEIPT, dict(payload))
            # after_sha 回填进 undo 记录：回滚之前要靠它判断"这个文件在那之后
            # 有没有被用户自己改过"，改过就必须先问一句。
            agent.run_store.update_undo(
                task_state.run_id, payload["action_id"], sha_after=dict(after_sha)
            )
            agent.last_action_receipt_id = payload["action_id"]
            return payload
        except Exception:
            return None
