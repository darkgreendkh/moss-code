"""结构化上下文压缩（spec-06 §4.1）。

为什么存在：20 步之后，前 14 步在 prompt 里是一堆被砍到 20 字符的碎片——
既占 token 又没有信息。真正需要的是一次**结构化**的交接：目标是什么、
已经做完了什么、哪些路已经排除了、发现了什么（带证据锚点）、还有什么没搞清。

这份摘要刻意做成 schema 而不是自由文本，理由是自由文本最容易把
"部分成功"写成"成功"——那是 compaction 最贵的一种错误。

四条硬性质：
- **可逆**：被压缩的原始条目整份写进 `.moss/runs/<id>/context/turns-<n>.jsonl`，
  摘要里附路径，模型可以用 `read_artifact` 取回。
- **幂等**：同一输入 + 同一 method + 同一 schema 版本，产出的 artifact 除
  `id`/`created_at` 外逐字段一致；已压过的区间再压不产生新 artifact。
- **闭合**：covered + kept = 全集。没有条目会被"顺手丢掉"。
- **因果单元不可拆**：最小单位是 (工具调用, 结果, 它触发的通知)。
  不允许出现有调用无结果的残缺组。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace

from .clock import now
from .token_budget import clip, clip_to_budget, estimate_tokens
# 直接导常量而不是导模块：本文件里有同名的 trace_events 形参（一段 trace 事件列表）。
from .runs.observability.events import STALL_DETECTED, TOOL_EXECUTED

COMPACTION_SCHEMA_VERSION = 1
# 最近这么多个执行步骤原样保留：刚发生的错误如果被摘要掉，
# 模型下一步就会原样再犯一次（spec-06 §10 开放问题 1）。
KEEP_RECENT_STEPS = 3
# 会改工作区的工具。completed 只从这些工具的**成功**调用里来。
WRITE_TOOLS = ("write_file", "edit_file", "run_shell")
# 一次"排除"的判定依据：这些错误码说明"此路不通"，不是"再试一次就行"。
EXCLUDING_ERROR_CODES = ("approval_denied", "capability_denied", "command_denied", "tool_not_allowed")
_ERROR_LINE = re.compile(
    r"(?i)^.*(error|failed|failure|traceback|exception|fatal|assert|denied|no such|not found).*$"
)


@dataclass(frozen=True)
class Finding:
    text: str
    # 证据锚点必填："path:line" 或 "event:<seq>"。没有锚点的"发现"没法复核，
    # 而不可复核的摘要正是 compaction 最容易骗人的地方。
    evidence: str

    def to_dict(self):
        return {"text": self.text, "evidence": self.evidence}


@dataclass(frozen=True)
class CompactionArtifact:
    id: str
    run_id: str
    covered_seq_start: int
    covered_seq_end: int
    method: str
    created_at: str
    raw_path: str
    before_tokens: int
    after_tokens: int
    covered_history_count: int
    kept_history_count: int
    schema_version: int = COMPACTION_SCHEMA_VERSION
    goals: tuple = ()
    completed: tuple = ()
    excluded: tuple = ()
    findings: tuple = ()
    open_questions: tuple = ()
    plan: tuple = ()

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "run_id": self.run_id,
            "covered_seq_start": self.covered_seq_start,
            "covered_seq_end": self.covered_seq_end,
            "covered_history_count": self.covered_history_count,
            "kept_history_count": self.kept_history_count,
            "method": self.method,
            "created_at": self.created_at,
            "goals": list(self.goals),
            "completed": list(self.completed),
            "excluded": list(self.excluded),
            "findings": [finding.to_dict() for finding in self.findings],
            "open_questions": list(self.open_questions),
            "plan": [dict(step) for step in self.plan],
            "raw_path": self.raw_path,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
        }

    def fingerprint(self):
        """除 id/created_at 外的全部内容摘要。幂等性就是按这个判定的。"""
        payload = self.to_dict()
        payload.pop("id", None)
        payload.pop("created_at", None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass
class _Extraction:
    goals: list = field(default_factory=list)
    completed: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    plan: list = field(default_factory=list)


def execution_steps(history):
    """把历史切成因果单元，返回 [[(index, item), ...], ...]。

    单元的边界是"一次调用连同它的结果和后续通知"：把调用和结果拆到压缩边界
    两侧，模型会看到一个没有下文的动作，然后理所当然地再做一遍。
    """
    steps = []
    pending = []
    for index, item in enumerate(history):
        role = str(item.get("role", ""))
        if role == "assistant" and item.get("native_tool_call"):
            pending.append((index, item))
            continue
        if role == "tool":
            steps.append([*pending, (index, item)])
            pending = []
            continue
        if role == "system" and steps and not pending:
            # runtime notice / 停滞干预属于"刚才那一步引发的事"，跟着它走。
            steps[-1].append((index, item))
            continue
        if pending:
            # 有调用无结果：单独成组，`compactable_steps` 会把它留在尾巴里。
            steps.append(pending)
            pending = []
        steps.append([(index, item)])
    if pending:
        steps.append(pending)
    return steps


def step_is_complete(step):
    calls = [item for _, item in step if item.get("native_tool_call")]
    results = [item for _, item in step if str(item.get("role", "")) == "tool"]
    return not calls or bool(results)


def compactable_steps(steps, keep_recent=KEEP_RECENT_STEPS):
    """返回 (可压缩的步骤, 保留的步骤)。

    尾部保留最近 N 步；再从可压缩的一侧往回退，直到最后一步是完整的因果单元。
    """
    cut = max(0, len(steps) - int(keep_recent))
    while cut > 0 and not step_is_complete(steps[cut - 1]):
        cut -= 1
    return steps[:cut], steps[cut:]


def _entries(steps):
    return [item for step in steps for _, item in step]


def _tool_entries(steps):
    return [item for item in _entries(steps) if str(item.get("role", "")) == "tool"]


def _event_action(event):
    args = dict(event.get("args", {}) or {})
    detail = str(args.get("path", "") or args.get("command", "") or args.get("pattern", ""))
    return f"{event.get('name', 'tool')} {detail}".strip()


def _error_lines(text, limit=2):
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    signal = [line for line in lines if _ERROR_LINE.match(line)]
    return (signal or lines)[:limit]


def _covered_events(trace_events, covered_tool_count):
    """取出属于被压缩区间的 trace 事件。

    工具事件和工具历史条目是同序的（`Action.index` 决定写回顺序，
    这是主循环的不变量），所以按数量切就够，不需要再对一次 args。
    """
    events = list(trace_events or [])
    tool_events = [event for event in events if event.get("event") == TOOL_EXECUTED]
    covered_tools = tool_events[:covered_tool_count]
    if not covered_tools:
        return [], 0, 0
    last_sequence = int(covered_tools[-1].get("sequence", 0) or 0)
    covered = [event for event in events if int(event.get("sequence", 0) or 0) <= last_sequence]
    first_sequence = int(covered[0].get("sequence", 0) or 0) if covered else 0
    return covered, first_sequence, last_sequence


def extract_rule(covered_entries, covered_events):
    """从 trace 聚合出结构化交接。零成本、可审计，是默认模式。"""
    extraction = _Extraction()

    for item in covered_entries:
        if str(item.get("role", "")) == "user":
            text = str(item.get("content", "")).strip()
            if text and text not in extraction.goals:
                extraction.goals.append(clip(text, 200))

    denied_paths = set()
    for event in covered_events:
        name = str(event.get("event", ""))
        if name == "plan_updated":
            extraction.plan = [dict(step) for step in event.get("steps", []) or []]
            continue
        if name == STALL_DETECTED:
            question = f"stalled ({event.get('kind', 'unknown')}): {event.get('detail', '')}".strip()
            if question not in extraction.open_questions:
                extraction.open_questions.append(clip(question, 200))
            continue
        if name != TOOL_EXECUTED:
            continue

        status = str(event.get("tool_status", ""))
        error_code = str(event.get("tool_error_code", "") or "")
        action = _event_action(event)
        sequence = int(event.get("sequence", 0) or 0)

        if error_code in EXCLUDING_ERROR_CODES:
            entry = f"{action} -> {error_code}"
            if entry not in extraction.excluded:
                extraction.excluded.append(entry)
            denied_paths.add(action)
            continue

        if status == "ok" and str(event.get("name", "")) in WRITE_TOOLS:
            paths = [str(path) for path in event.get("affected_paths", []) or []]
            entry = f"{action} -> changed {', '.join(paths)}" if paths else action
            if entry not in extraction.completed:
                extraction.completed.append(entry)
            continue

        if status == "partial_success":
            # 半成品绝不能进 completed。它进的是"还没解决的问题"那一栏，
            # 并且明确写着 partial —— 把它摘要成成功是这套机制最贵的错误。
            note = f"{action} -> partial_success ({error_code or 'partially applied'})"
            if note not in extraction.open_questions:
                extraction.open_questions.append(note)
            extraction.findings.append(
                Finding(text=clip(note, 200), evidence=f"event:{sequence}")
            )
            continue

        if status == "error" or error_code:
            for line in _error_lines(event.get("result", "")):
                extraction.findings.append(
                    Finding(text=clip(f"{action} failed: {line}", 200), evidence=f"event:{sequence}")
                )
            continue

        if str(event.get("name", "")) == "read_file":
            args = dict(event.get("args", {}) or {})
            path = str(args.get("path", "")).strip()
            if not path:
                continue
            summary = summarize_read(str(event.get("result", "")))
            if summary:
                extraction.findings.append(
                    Finding(
                        text=clip(f"{path}: {summary}", 200),
                        evidence=f"{path}:{int(args.get('start', 1) or 1)}",
                    )
                )

    for step in extraction.plan:
        if str(step.get("status", "")) not in {"done"}:
            question = f"plan step {step.get('id', '?')} not done: {step.get('title', '')}"
            if question not in extraction.open_questions:
                extraction.open_questions.append(question)

    if denied_paths:
        extraction.open_questions.append(
            "some approaches were refused; pick a different one instead of retrying them"
        )
    return extraction


def summarize_read(result, limit=160):
    """复用 spec-05 的读文件摘要口径，避免出现第二套"读到了什么"的说法。"""
    from .memory.service import summarize_read_result

    return summarize_read_result(result, limit=limit)


def compact(
    history,
    trace_events,
    *,
    method="rule",
    budget=1200,
    aux_client=None,
    run_id="",
    raw_path="",
    keep_recent=KEEP_RECENT_STEPS,
    measure=estimate_tokens,
    created_at=None,
    artifact_id=None,
):
    """把历史压成一份结构化交接。

    返回 `(artifact, remaining_history)`：`remaining_history` 是**没有被压缩**
    的那些条目（最近 N 个完整执行步骤）。调用方负责把摘要放回历史开头——
    压缩本身不碰任何持久化状态，这样它可以被反复调用、被测试直接验证。

    没有可压缩的内容时返回 `(None, history)`：对已压过的区间再压不产生新 artifact。
    """
    history = list(history or [])
    steps = execution_steps(history)
    compacted, kept = compactable_steps(steps, keep_recent=keep_recent)
    covered_entries = _entries(compacted)
    if not covered_entries:
        return None, history

    covered_events, seq_start, seq_end = _covered_events(trace_events, len(_tool_entries(compacted)))
    if method == "model" and aux_client is not None:
        extraction = extract_model(covered_entries, covered_events, aux_client=aux_client, budget=budget)
        used_method = "model" if extraction is not None else "rule"
        extraction = extraction or extract_rule(covered_entries, covered_events)
    else:
        # method="model" 但没有 aux client 时诚实地记成 rule，
        # 而不是给一份贴着 model 标签的规则产物。
        extraction = extract_rule(covered_entries, covered_events)
        used_method = "rule"

    before_tokens = sum(measure(str(item.get("content", ""))) for item in covered_entries)
    artifact = CompactionArtifact(
        id=artifact_id or _artifact_id(covered_entries, used_method),
        run_id=str(run_id),
        covered_seq_start=seq_start,
        covered_seq_end=seq_end,
        covered_history_count=len(covered_entries),
        kept_history_count=len(_entries(kept)),
        method=used_method,
        created_at=created_at or now(),
        raw_path=str(raw_path),
        before_tokens=before_tokens,
        after_tokens=0,
        goals=tuple(extraction.goals),
        completed=tuple(extraction.completed),
        excluded=tuple(extraction.excluded),
        findings=tuple(extraction.findings),
        open_questions=tuple(extraction.open_questions),
        plan=tuple(dict(step) for step in extraction.plan),
    )
    artifact = replace(artifact, after_tokens=measure(render_compaction(artifact, budget)))
    return artifact, _entries(kept)


def extract_model(covered_entries, covered_events, *, aux_client, budget):
    """模型模式：aux model 一次调用，输出必须是同一份 schema。

    输入是规则模式的产物加上原始历史的指针，而不是把整段历史再灌一遍——
    压缩的目的就是省 token，为它花掉一份全量输入是本末倒置。

    三道校验，全部是**代码级**的，不靠 prompt 里的措辞：
    - 解析不出 JSON 对象 → 退回规则模式（读不懂的 JSON 比朴素摘要糟糕得多）；
    - `completed` 只能从规则模式的集合里选：规则模式永远不会把 partial_success
      放进 completed，于是"把半成品说成做完了"在结构上就不可能发生；
    - findings 的证据锚点必须是规则模式见过的那些，编造的一律丢弃。
    """
    baseline = extract_rule(covered_entries, covered_events)
    prompt = _model_prompt(baseline, covered_entries, budget)
    try:
        raw = aux_client.complete(prompt, max(256, int(budget)))
    except Exception:
        return None
    payload = _parse_json_object(raw)
    if payload is None:
        return None

    allowed_completed = set(baseline.completed)
    allowed_evidence = {finding.evidence for finding in baseline.findings}
    try:
        findings = [
            Finding(text=str(item["text"]), evidence=str(item["evidence"]))
            for item in payload.get("findings", [])
            if str(item.get("text", "")).strip() and str(item.get("evidence", "")) in allowed_evidence
        ]
        completed = [
            str(item) for item in payload.get("completed", []) if str(item) in allowed_completed
        ]
    except (AttributeError, KeyError, TypeError):
        return None
    return _Extraction(
        goals=[str(item) for item in payload.get("goals", []) if str(item).strip()] or baseline.goals,
        completed=completed,
        excluded=[str(item) for item in payload.get("excluded", []) if str(item).strip()] or baseline.excluded,
        findings=findings or baseline.findings,
        # open_questions 允许模型自由发挥：漏一条待办的代价，
        # 远小于把一个未解决的问题说成已解决。
        open_questions=[str(item) for item in payload.get("open_questions", []) if str(item).strip()]
        or baseline.open_questions,
        plan=baseline.plan,
    )


def _model_prompt(baseline, covered_entries, budget):
    rule_summary = json.dumps(
        {
            "goals": baseline.goals,
            "completed": baseline.completed,
            "excluded": baseline.excluded,
            "findings": [finding.to_dict() for finding in baseline.findings],
            "open_questions": baseline.open_questions,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        "You are compacting an agent run's transcript into a structured handoff.\n"
        "Return ONLY a JSON object with keys: goals, completed, excluded, findings, open_questions.\n"
        "findings is a list of {text, evidence}; evidence must be a 'path:line' or 'event:<seq>' anchor "
        "taken from the rule-based draft. Never invent evidence: unknown anchors are dropped.\n"
        "completed may only contain entries that already appear in the draft's completed list; "
        "anything else is dropped. Never describe a partial_success as a success.\n"
        f"There are {len(covered_entries)} compacted transcript entries.\n"
        f"Rule-based draft:\n{clip_to_budget(rule_summary, max(400, int(budget)), measure=len)}\n"
    )


def _parse_json_object(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _artifact_id(covered_entries, method):
    digest = hashlib.sha256(
        json.dumps(
            [
                {
                    "role": item.get("role"),
                    "name": item.get("name"),
                    "content": item.get("content"),
                }
                for item in covered_entries
            ],
            sort_keys=True,
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"cmp_{method}_{digest}"


def render_compaction(artifact, budget):
    """把 artifact 渲染成进 prompt 的那段文本。

    每一栏都带明确的语义标签，尤其是 "excluded"——它的作用是防止模型
    把已经排除过的方案再试一遍，这是长任务里最常见的一种空转。
    """
    if artifact is None:
        return ""
    lines = [
        f"Context summary (compacted {artifact.covered_history_count} earlier entries, "
        f"events {artifact.covered_seq_start}-{artifact.covered_seq_end}, method={artifact.method}):"
    ]
    if artifact.raw_path:
        lines.append(
            f'- Full transcript kept at {artifact.raw_path}; '
            f'read it with read_artifact("{artifact.raw_path}", start, end).'
        )
    if artifact.goals:
        lines.append("- Goals: " + " | ".join(artifact.goals))
    if artifact.completed:
        lines.append("- Completed: " + " | ".join(artifact.completed))
    if artifact.excluded:
        lines.append("- Excluded (already ruled out, do not retry): " + " | ".join(artifact.excluded))
    for finding in artifact.findings:
        lines.append(f"- Finding: {finding.text} [{finding.evidence}]")
    if artifact.open_questions:
        lines.append("- Open questions: " + " | ".join(artifact.open_questions))
    if artifact.plan:
        lines.append(
            "- Plan: "
            + " | ".join(f"{step.get('id', '?')} {step.get('title', '')} [{step.get('status', '?')}]" for step in artifact.plan)
        )
    return clip_to_budget("\n".join(lines), int(budget), measure=len, keep="head")
