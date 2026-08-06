"""动作意图 / 回执，以及崩溃后的对账。

为什么存在（spec-07 §4.6）：
工具是**先产生副作用、再写记录**的。这中间有一个窗口：文件已经写下去了，但
history / trace / checkpoint 还没落盘。崩在这个窗口里，恢复时看到的是一次
"没有任何痕迹的动作"——既不知道它做没做，也就不敢重放（可能写两次）、
不敢跳过（可能一次都没写）。

解法是把一次有副作用的动作拆成两条 trace 事件：执行前一条 `action_intent`
（我准备做什么、目标文件现在是什么样、做完应该是什么样），执行后一条
`action_receipt`（做成了没有、改了哪些文件、结果是什么样）。恢复时按 action_id
配对：
- 有 intent 有 receipt         -> 完成了，跳过
- 无 intent                    -> 没开始，正常继续
- 有 intent 无 receipt，幂等工具 -> 拿当前文件指纹去比对，判"已生效"还是"可重放"
- 有 intent 无 receipt，非幂等   -> **不自动重放**，标 pending_unknown 交给人确认

最后一条是刻意的：run_shell 重放一次可能是再发一封邮件、再删一次库。
宁可多问一次。

只对 risky 工具记账：只读工具没有副作用，为它们记账只是让 trace 体积翻倍；
而且只读工具批可以并发，两条事件跨线程写 trace 反而引入新的不确定性。
"""

import hashlib
import json
import uuid

from . import trace_events

# 这些工具的副作用完全由参数决定：同样的参数重放两次，结果和一次一样。
# 所以崩溃窗口里可以靠"当前文件是什么样"反推它到底生效了没有。
IDEMPOTENT_TOOLS = frozenset({"write_file", "edit_file"})

# 对账结论。
STATUS_COMPLETED = "completed"
STATUS_ALREADY_APPLIED = "already_applied"
STATUS_REPLAYABLE = "replayable"
STATUS_PENDING_UNKNOWN = "pending_unknown"


def new_action_id():
    return "act_" + uuid.uuid4().hex[:12]


def args_digest(args):
    text = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_text(text):
    return "sha256:" + hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def file_sha(path):
    """文件内容指纹。文件不存在返回 None —— 和"空文件"是两回事。"""
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def idempotency_key(previous_receipt_id, digest):
    """同一个动作重放时算出同一把钥匙，于是不会产生两条 receipt。"""
    seed = f"{previous_receipt_id or ''}|{digest}"
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def intended_sha(name, args, resolve_path):
    """这次动作做完之后，目标文件**应该**是什么内容指纹。

    没有它，"有 intent 无 receipt"就只能一律转人工：光看执行前的指纹，
    分不出"还没写"和"写完又被改回去了"。写类工具的目标内容完全由参数决定，
    所以这里能提前算出来——这正是幂等工具可以自动对账的原因。
    """
    if name == "write_file":
        return _sha256_text(args.get("content", ""))
    if name == "edit_file":
        try:
            path = resolve_path(str(args.get("path", "")))
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        old_text = str(args.get("old_text", ""))
        if not old_text or text.count(old_text) != 1:
            return None
        return _sha256_text(text.replace(old_text, str(args.get("new_text", "")), 1))
    return None


def build_intent(
    *,
    name,
    args,
    capabilities=(),
    risk="low",
    call_id="",
    expected_sha=None,
    approval_receipt_id="",
    previous_receipt_id="",
    resolve_path=None,
):
    digest = args_digest(args)
    return {
        "action_id": new_action_id(),
        "call_id": str(call_id or ""),
        "tool": str(name),
        "args_digest": digest,
        "idempotency_key": idempotency_key(previous_receipt_id, digest),
        "capabilities": sorted(str(item) for item in (capabilities or ())),
        "risk": str(risk),
        "expected_sha": dict(expected_sha or {}),
        "intended_sha": intended_sha(name, args or {}, resolve_path) if resolve_path else None,
        "approval_receipt_id": str(approval_receipt_id or ""),
        "idempotent": str(name) in IDEMPOTENT_TOOLS,
    }


def build_receipt(intent, *, status, exit_code=None, affected_paths=(), after_sha=None, duration_ms=0):
    return {
        "action_id": str(intent.get("action_id", "")),
        "idempotency_key": str(intent.get("idempotency_key", "")),
        "tool": str(intent.get("tool", "")),
        "status": str(status),
        "exit_code": exit_code,
        "affected_paths": list(affected_paths or []),
        "after_sha": dict(after_sha or {}),
        "duration_ms": int(duration_ms or 0),
    }


def pair_actions(events):
    """把 trace 里的 intent / receipt 按 action_id 配对。

    返回 (已配对的 action_id 集合, 缺 receipt 的 intent 列表)。
    """
    intents = {}
    order = []
    receipts = set()
    for event in events or []:
        name = str(event.get("event", ""))
        if name == trace_events.ACTION_INTENT:
            action_id = str(event.get("action_id", ""))
            if action_id and action_id not in intents:
                intents[action_id] = event
                order.append(action_id)
        elif name == trace_events.ACTION_RECEIPT:
            receipts.add(str(event.get("action_id", "")))
    pending = [intents[action_id] for action_id in order if action_id not in receipts]
    return receipts, pending


def reconcile(events, resolve_path):
    """崩溃后对账：每个缺 receipt 的 intent 现在算什么状态。

    `resolve_path` 把 intent 里记的相对路径变回工作区里的绝对路径 —— 对账必须
    看**当前磁盘**，而不是相信任何记录。
    """
    _, pending = pair_actions(events)
    outcomes = []
    for intent in pending:
        outcomes.append(_classify(intent, resolve_path))
    return outcomes


def _classify(intent, resolve_path):
    tool = str(intent.get("tool", ""))
    outcome = {
        "action_id": str(intent.get("action_id", "")),
        "tool": tool,
        "idempotency_key": str(intent.get("idempotency_key", "")),
        "paths": sorted(dict(intent.get("expected_sha", {}) or {})),
    }
    if not intent.get("idempotent"):
        # 非幂等工具一律转人工。重放 run_shell 可能是再删一次库。
        outcome["status"] = STATUS_PENDING_UNKNOWN
        outcome["reason"] = "non_idempotent_tool"
        return outcome
    expected = dict(intent.get("expected_sha", {}) or {})
    target = str(intent.get("intended_sha", "") or "")
    if not expected or not target:
        outcome["status"] = STATUS_PENDING_UNKNOWN
        outcome["reason"] = "no_recorded_fingerprint"
        return outcome
    for rel_path, before in expected.items():
        try:
            current = file_sha(resolve_path(rel_path))
        except Exception:
            outcome["status"] = STATUS_PENDING_UNKNOWN
            outcome["reason"] = "path_unresolvable"
            return outcome
        if current == target:
            outcome["status"] = STATUS_ALREADY_APPLIED
            outcome["reason"] = "target_content_present"
            return outcome
        if current == before:
            outcome["status"] = STATUS_REPLAYABLE
            outcome["reason"] = "unchanged_since_intent"
            return outcome
    # 既不是"写之前"也不是"写之后"：这中间有第三方改过它，自动判定不再安全。
    outcome["status"] = STATUS_PENDING_UNKNOWN
    outcome["reason"] = "third_party_change"
    return outcome


def summarize(outcomes):
    """给 `--explain` 和恢复提示用的一句话摘要。"""
    counts = {}
    for outcome in outcomes or []:
        counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
    return counts
