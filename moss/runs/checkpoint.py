"""Checkpoint and resume-state helpers."""

import copy
import uuid

from . import ledger as action_ledger
from ..clock import now
from ..memory import service as memorylib
from ..token_budget import clip
from ..workspace import WORKSPACE_FINGERPRINT_VERSION

# 每执行一步工具就会生成一个 checkpoint，且它们全都存在 session JSON 里、
# 每次保存都整份重写。不设上限的话，一个长期的 REPL 会话里 checkpoint 会无限
# 累积，session 文件越来越大、保存越来越慢。只有最新的 current checkpoint 会被
# 读取用于恢复，历史 checkpoint 仅作参考，所以保留最近 N 个足矣。
CHECKPOINT_HISTORY_LIMIT = 40

CHECKPOINT_SCHEMA_VERSION = "phase1-v1"
CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_FULL_VALID_STATUS = "full-valid"
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"
CHECKPOINT_SCHEMA_MISMATCH_STATUS = "schema-mismatch"

RUNTIME_IDENTITY_KEYS = (
    "cwd",
    "model",
    "model_client",
    "approval_policy",
    "read_only",
    "max_steps",
    "max_new_tokens",
    "feature_flags",
    "shell_env_allowlist",
    "workspace_fingerprint",
    "tool_signature",
    "prompt_version",
)


def current_runtime_identity(agent):
    return {
        "session_id": agent.session.get("id", ""),
        "cwd": str(agent.root),
        "model": str(getattr(agent.model_client, "model", "")),
        "model_client": agent.model_client.__class__.__name__,
        "approval_policy": agent.approval_policy,
        "read_only": bool(agent.read_only),
        "max_steps": int(agent.max_steps),
        "max_new_tokens": int(agent.max_new_tokens),
        "feature_flags": dict(agent.feature_flags),
        "shell_env_allowlist": list(agent.shell_env_allowlist),
        "workspace_fingerprint": getattr(getattr(agent, "prefix_state", None), "workspace_fingerprint", agent.workspace.fingerprint()),
        "tool_signature": agent.tool_signature(),
        "prompt_version": getattr(getattr(agent, "prefix_state", None), "prompt_version", ""),
    }


def checkpoint_state(agent):
    agent._ensure_session_shape()
    return agent.session["checkpoints"]


def current_checkpoint(agent):
    state = checkpoint_state(agent)
    checkpoint_id = str(state.get("current_id", "")).strip()
    if not checkpoint_id:
        return None
    return state.get("items", {}).get(checkpoint_id)


def _stale_fingerprint_version(checkpoint):
    """checkpoint 里存的 workspace 指纹是不是旧口径算出来的。

    指纹算法一改，所有历史 checkpoint 的指纹都对不上。如果不识别版本前缀，
    这会表现成 `workspace-mismatch`——误导用户去找“工作区被谁改了”，
    而真实原因是 schema 变了。所以前缀不同就直接判 schema-mismatch。
    """
    saved = str(dict(checkpoint.get("runtime_identity", {}) or {}).get("workspace_fingerprint", ""))
    if not saved:
        return False
    return not saved.startswith(f"{WORKSPACE_FINGERPRINT_VERSION}:")


def evaluate_resume_state(agent):
    previous_resume_state = dict(agent.session.get("resume_state", {}) or {})
    interrupted_runs = list(getattr(agent, "interrupted_runs", []) or previous_resume_state.get("interrupted_runs", []) or [])
    invalidated = agent.invalidate_stale_memory()
    checkpoint = current_checkpoint(agent)
    status = CHECKPOINT_NONE_STATUS
    stale_paths = list(invalidated)
    mismatch_fields = []
    if checkpoint:
        if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION or _stale_fingerprint_version(checkpoint):
            status = CHECKPOINT_SCHEMA_MISMATCH_STATUS
        else:
            for item in checkpoint.get("key_files", []):
                path = str(item.get("path", "")).strip()
                if not path:
                    continue
                expected = item.get("freshness")
                current = memorylib.file_freshness(path, agent.root)
                if expected != current and path not in stale_paths:
                    stale_paths.append(path)
            saved_identity = dict(checkpoint.get("runtime_identity", {}) or agent.session.get("runtime_identity", {}) or {})
            current_identity = current_runtime_identity(agent)
            for key in RUNTIME_IDENTITY_KEYS:
                if key not in saved_identity:
                    continue
                if saved_identity.get(key) != current_identity.get(key):
                    mismatch_fields.append(key)
            mismatch_fields.sort()
            if stale_paths:
                status = CHECKPOINT_PARTIAL_STALE_STATUS
            elif mismatch_fields:
                status = CHECKPOINT_WORKSPACE_MISMATCH_STATUS
            else:
                status = CHECKPOINT_FULL_VALID_STATUS

    resume_state = {
        "status": status,
        "stale_paths": stale_paths,
        "runtime_identity_mismatch_fields": mismatch_fields,
        "stale_summary_invalidations": max(
            len(invalidated),
            int(previous_resume_state.get("stale_summary_invalidations", 0))
            if status == CHECKPOINT_PARTIAL_STALE_STATUS
            else 0,
        ),
    }
    if interrupted_runs:
        resume_state["interrupted_runs"] = interrupted_runs
    agent.session["resume_state"] = resume_state
    agent.session["runtime_identity"] = current_runtime_identity(agent)
    return resume_state


def _interrupted_run_lines(interrupted_runs):
    lines = []
    for item in interrupted_runs:
        last_event = dict(item.get("last_complete_event", {}) or {})
        event_name = str(last_event.get("event", "")).strip() or "-"
        line = f"{item.get('run_id', '-')}: last complete event {event_name}"
        # 崩溃窗口里"不知道做没做"的动作必须说出来。模型看不到它，
        # 就会照着"没做过"继续推进，而磁盘上可能已经改了。
        unknown = [
            outcome
            for outcome in item.get("pending_actions", []) or []
            if outcome.get("status") == action_ledger.STATUS_PENDING_UNKNOWN
        ]
        if unknown:
            detail = ", ".join(
                f"{outcome['tool']}({outcome.get('reason', '')})" for outcome in unknown
            )
            line += f"; unverified actions: {detail}"
        lines.append(line)
    return lines


def render_checkpoint_text(agent):
    checkpoint = current_checkpoint(agent)
    interrupted_runs = list(agent.resume_state.get("interrupted_runs", []) or [])
    # 计划渲染在最前面：它是模型自己写的意图声明，放在 checkpoint 段顶部
    # 最容易被对照着看"我说要做的和我正在做的一致吗"。
    plan_text = agent.render_plan_text() if hasattr(agent, "render_plan_text") else ""
    if not checkpoint:
        lines = []
        if plan_text:
            lines.append(plan_text)
        if interrupted_runs:
            lines.extend(
                [
                    "Task checkpoint:",
                    f"- Resume status: {agent.resume_state.get('status', CHECKPOINT_NONE_STATUS)}",
                    "- Interrupted runs: " + " | ".join(_interrupted_run_lines(interrupted_runs)),
                ]
            )
        return "\n".join(lines)
    lines = [
        "Task checkpoint:",
        f"- Resume status: {agent.resume_state.get('status', CHECKPOINT_NONE_STATUS)}",
        f"- Current goal: {checkpoint.get('current_goal', '-') or '-'}",
        f"- Current blocker: {checkpoint.get('current_blocker', '-') or '-'}",
        f"- Next step: {checkpoint.get('next_step', '-') or '-'}",
    ]
    key_files = [str(item.get("path", "")).strip() for item in checkpoint.get("key_files", []) if str(item.get("path", "")).strip()]
    lines.append(f"- Key files: {', '.join(key_files) or '-'}")
    if checkpoint.get("completed"):
        lines.append("- Completed: " + " | ".join(str(item) for item in checkpoint.get("completed", [])))
    if checkpoint.get("excluded"):
        lines.append("- Excluded: " + " | ".join(str(item) for item in checkpoint.get("excluded", [])))
    if agent.resume_state.get("stale_paths"):
        lines.append("- Stale paths: " + ", ".join(agent.resume_state["stale_paths"]))
    if interrupted_runs:
        lines.append("- Interrupted runs: " + " | ".join(_interrupted_run_lines(interrupted_runs)))
    summary = str(checkpoint.get("summary", "")).strip()
    if summary:
        lines.append(f"- Summary: {summary}")
    if plan_text:
        return plan_text + "\n\n" + "\n".join(lines)
    return "\n".join(lines)


def checkpoint_tree(session):
    """按 parent_checkpoint_id 组出 checkpoint 的父子关系。

    这个字段一直存在但从来没被用过。组成树之后，`--fork` 才能回答
    "从哪一步分叉出去"，而不是只能从最新那一步继续。
    """
    items = dict((session.get("checkpoints", {}) or {}).get("items", {}) or {})
    children = {}
    for checkpoint_id, checkpoint in items.items():
        parent = str(checkpoint.get("parent_checkpoint_id", "") or "")
        children.setdefault(parent, []).append(checkpoint_id)
    return {"items": items, "children": children, "roots": children.get("", [])}


def fork_session(session, checkpoint_id, new_session_id):
    """从某个 checkpoint 分叉出一个新 session，返回新的 session dict。

    只继承状态，不继承回滚能力：undo 记录挂在原来那次 run 上，复制过来会让
    `/rewind` 去改一份它从没写过的工作区。这一点写进 `forked_from`，
    而不是让用户以为还能 rewind 回去。
    """
    items = dict((session.get("checkpoints", {}) or {}).get("items", {}) or {})
    checkpoint = items.get(str(checkpoint_id))
    if checkpoint is None:
        raise KeyError(f"unknown checkpoint: {checkpoint_id}")
    history = list(session.get("history", []) or [])
    # 历史裁到分叉点：checkpoint 是"那一刻的状态"，把它之后的历史带过来
    # 就等于分叉出一条本来不存在的时间线。
    keep = int(checkpoint.get("history_length", len(history)) or 0)
    ancestry = []
    cursor = str(checkpoint_id)
    while cursor and cursor in items:
        ancestry.append(cursor)
        cursor = str(items[cursor].get("parent_checkpoint_id", "") or "")
    return {
        "id": str(new_session_id),
        "created_at": now(),
        "workspace_root": session.get("workspace_root", ""),
        "history": history[:keep],
        "memory": copy.deepcopy(dict(session.get("memory", {}) or {})),
        "checkpoints": {
            "current_id": str(checkpoint_id),
            "items": {key: items[key] for key in reversed(ancestry)},
        },
        "runtime_identity": dict(checkpoint.get("runtime_identity", {}) or {}),
        "forked_from": {
            "session_id": session.get("id", ""),
            "checkpoint_id": str(checkpoint_id),
            "inherits_undo": False,
        },
    }


RESUME_PART_NAMES = ("memory", "plan", "history", "checkpoint")


def parse_resume_parts(raw):
    """`--resume-parts=memory,plan` -> 一个集合。空/None = 全部。"""
    if raw in (None, "", "all"):
        return set(RESUME_PART_NAMES)
    parts = {item.strip() for item in str(raw).split(",") if item.strip()}
    unknown = sorted(parts - set(RESUME_PART_NAMES))
    if unknown:
        raise ValueError(
            f"unknown resume part: {', '.join(unknown)}; expected any of {', '.join(RESUME_PART_NAMES)}"
        )
    return parts


def apply_resume_parts(session, parts):
    """按用户选的部分裁剪一份待恢复的 session。

    为什么要能只恢复一部分：上次跑歪了的时候，用户往往想留住"读过哪些文件"
    （memory）却丢掉那段把自己绕进去的对话（history）。全有或全无的恢复，
    实际结果是用户干脆不恢复。
    """
    parts = set(parts or RESUME_PART_NAMES)
    session = dict(session or {})
    if "memory" not in parts:
        session["memory"] = memorylib.default_memory_state()
    if "history" not in parts:
        session["history"] = []
    if "checkpoint" not in parts:
        session["checkpoints"] = {"current_id": "", "items": {}}
    if "plan" not in parts:
        checkpoints = dict(session.get("checkpoints", {}) or {})
        items = {}
        for key, checkpoint in dict(checkpoints.get("items", {}) or {}).items():
            trimmed = dict(checkpoint)
            trimmed["plan"] = []
            items[key] = trimmed
        checkpoints["items"] = items
        session["checkpoints"] = checkpoints
        session["plan"] = []
    return session


def explain_resume(agent):
    """`--resume --explain` 的内容：恢复之后会发生什么。

    三个问题必须当场回答，否则"恢复"就是一次盲跳：
    哪些文件在我离开之后变了、运行环境有哪些字段对不上、
    以及**会不会重放有副作用的动作**。
    """
    resume_state = dict(agent.resume_state or {})
    checkpoint = current_checkpoint(agent)
    stale_paths = list(resume_state.get("stale_paths", []) or [])
    freshness = []
    for item in (checkpoint or {}).get("key_files", []) or []:
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        freshness.append(
            {
                "path": path,
                "recorded": item.get("freshness"),
                "current": memorylib.file_freshness(path, agent.root),
                "stale": path in stale_paths,
            }
        )
    saved_identity = dict((checkpoint or {}).get("runtime_identity", {}) or {})
    current_identity = current_runtime_identity(agent)
    mismatches = [
        {"field": field, "saved": saved_identity.get(field), "current": current_identity.get(field)}
        for field in resume_state.get("runtime_identity_mismatch_fields", []) or []
    ]
    pending = []
    for run in resume_state.get("interrupted_runs", []) or []:
        for outcome in run.get("pending_actions", []) or []:
            pending.append({"run_id": run.get("run_id", ""), **outcome})
    return {
        "session_id": agent.session.get("id", ""),
        "status": resume_state.get("status", CHECKPOINT_NONE_STATUS),
        "checkpoint_id": (checkpoint or {}).get("checkpoint_id", ""),
        "freshness": freshness,
        "stale_paths": stale_paths,
        "runtime_identity_mismatch": mismatches,
        "pending_actions": pending,
        "replays_side_effects": any(
            outcome.get("status") == action_ledger.STATUS_REPLAYABLE for outcome in pending
        ),
    }


def render_explain(explanation):
    """把 explain_resume 的结果渲染成人话。"""
    lines = [
        f"session: {explanation['session_id']}",
        f"resume status: {explanation['status']}",
        f"checkpoint: {explanation['checkpoint_id'] or '-'}",
        "",
        "freshness (recorded vs now):",
    ]
    if explanation["freshness"]:
        for item in explanation["freshness"]:
            mark = "STALE" if item["stale"] else "ok"
            lines.append(f"  [{mark}] {item['path']}: {item['recorded']} -> {item['current']}")
    else:
        lines.append("  (no key files recorded)")
    lines.append("")
    lines.append("runtime identity mismatches:")
    if explanation["runtime_identity_mismatch"]:
        for item in explanation["runtime_identity_mismatch"]:
            lines.append(f"  {item['field']}: {item['saved']!r} -> {item['current']!r}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("actions that were never confirmed:")
    if explanation["pending_actions"]:
        for item in explanation["pending_actions"]:
            lines.append(
                f"  {item.get('run_id', '-')} {item.get('tool', '-')}: "
                f"{item.get('status', '-')} ({item.get('reason', '-')})"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(
        "resuming will replay a side-effecting action: "
        + ("yes" if explanation["replays_side_effects"] else "no")
    )
    return "\n".join(lines)


def infer_next_step(task_state):
    if task_state.status == "completed":
        return "No next step recorded."
    if task_state.stop_reason == "step_limit_reached":
        return "Resume from the latest checkpoint and continue the task."
    if task_state.last_tool:
        return f"Decide the next action after {task_state.last_tool}."
    return "Continue the task from the latest checkpoint."


def _prune_checkpoints(state, limit=CHECKPOINT_HISTORY_LIMIT):
    # 只保留最近插入的 limit 个 checkpoint（dict 保序，靠后 = 更新）。
    # current_id 一定是最后插入的，所以永远在保留集合里。
    items = state.get("items", {})
    if len(items) <= limit:
        return
    keep_keys = list(items)[-limit:]
    current_id = str(state.get("current_id", "")).strip()
    if current_id and current_id not in keep_keys:
        keep_keys.append(current_id)
    state["items"] = {key: items[key] for key in keep_keys if key in items}


def create_checkpoint(agent, task_state, user_message, trigger):
    state = checkpoint_state(agent)
    current = current_checkpoint(agent)
    checkpoint_id = "ckpt_" + uuid.uuid4().hex[:8]
    key_files = []
    freshness = {}
    for path in agent.memory.to_dict()["working"]["recent_files"]:
        file_freshness = memorylib.file_freshness(path, agent.root)
        freshness[path] = file_freshness
        key_files.append({"path": path, "freshness": file_freshness})
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": current.get("checkpoint_id", "") if current else "",
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "created_at": now(),
        "current_goal": str(user_message),
        "completed": [task_state.final_answer] if task_state.final_answer else [],
        "excluded": [],
        "current_blocker": "" if str(task_state.stop_reason or "") in ("", "final_answer_returned") else str(task_state.stop_reason),
        "next_step": infer_next_step(task_state),
        "key_files": key_files,
        "freshness": freshness,
        "summary": f"{trigger}: {clip(str(user_message), 120)}",
        # 计划一起存：它是模型自己写下的意图声明，属于"可恢复的状态"。
        # 不存的话 --resume 回来的 agent 会忘掉自己刚才打算怎么做。
        "plan": list(getattr(agent, "current_plan", []) or []),
        # 这一刻的历史长度。--fork 靠它把历史裁到分叉点，
        # /rewind 靠它把 history 截回第 n 步结束时的样子。
        "history_length": len(agent.session.get("history", []) or []),
        # 当时的 memory 快照。/rewind 要连 memory 一起回滚，否则文件退回去了，
        # 记忆里却还留着"我已经改好了 parser.py"这类已经不成立的事实。
        # 必须深拷贝：浅拷贝会让后续的 memory 写入顺手改掉这份"历史快照"。
        "memory": copy.deepcopy(agent.memory.to_dict()),
        "runtime_identity": current_runtime_identity(agent),
    }
    state["items"][checkpoint_id] = checkpoint
    state["current_id"] = checkpoint_id
    _prune_checkpoints(state)
    task_state.checkpoint_id = checkpoint_id
    agent.session["runtime_identity"] = checkpoint["runtime_identity"]
    agent.session_path = agent.session_store.save(agent.session)
    return checkpoint
