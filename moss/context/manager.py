"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。

预算的计量单位是「估算 token」而不是字符数（见 `token_budget`）：真实约束
是模型上下文窗口和费用，都按 token 计，而 CJK 文本的 token 密度远高于英文，
用字符数当预算会系统性低估中文内容的开销。计量函数可通过 `measure` 注入，
测试里会传 `len` 以获得确定的字符级行为。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import health as healthlib
from . import history as historylib
from . import native_history
from .model_request import Block, CompactionArtifact, Message, ModelRequest, PromptBundle
from .token_budget import clip_to_budget, estimate_tokens


# 预算与 floor 以「估算 token」为单位。
DEFAULT_TOTAL_BUDGET = 12000
DEFAULT_SECTION_BUDGETS = {
    "prefix": 3000,
    "memory": 700,
    "relevant_memory": 800,
    "history": 6000,
    # constraints 从原来 memory 的 1000 里划 300：它是"这一轮必须遵守什么"，
    # 和"以前发生过什么"不是一类信息，混在一段里前者会被后者挤掉。
    "constraints": 300,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 1000,
    "memory": 250,
    "relevant_memory": 200,
    "history": 1500,
    "constraints": 150,
}
# 当 prompt 超预算时，会优先压缩这些 section。
# constraints 排在最后：硬约束被削掉之后，模型看起来还在工作，
# 但它已经不知道自己该守什么了 —— 这是最贵的一种"省 token"。
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix", "constraints")
# 段落顺序（spec-06 §4.5）：稳定的在前（进缓存段），硬约束靠近末尾（模型对
# 结尾的注意力更强），当前请求永远最后。
SECTION_ORDER = (
    "prefix",
    "history",
    "memory",
    "relevant_memory",
    "constraints",
    "current_request",
)
CURRENT_REQUEST_SECTION = "current_request"
CONSTRAINTS_SECTION = "constraints"

# ---- 预算按模型窗口推导（spec-06 §4.6） ----
# 12000 是 2024 年的数字，而 2026 的窗口普遍 200K+：写死等于主动只用 6%。
# 但也不能把窗口用满——留给输出的余量、以及"一次请求别太大"的成本约束都是真的。
DEFAULT_CONTEXT_RATIO = 0.5
DEFAULT_CONTEXT_HARD_CAP = 60000
MIN_OUTPUT_RESERVE = 1024
MIN_DERIVED_BUDGET = 2000
# 各段占比。prefix 这一段在实现里同时装稳定头和 workspace/repo_map，
# 所以是 spec 里 prefix 25% + workspace 10% 的合计。
SECTION_SHARES = {
    "prefix": 0.35,
    "history": 0.45,
    "memory": 0.08,
    "relevant_memory": 0.07,
    CONSTRAINTS_SECTION: 0.05,
}
# 任务阶段微调：探索期主要在读，历史值钱；编辑期在改，相关文件和约束更值钱。
PHASE_SHARE_SHIFT = 0.05
# 每段的用途说明。模型不会自动知道"这段是历史所以只是参考、那段是约束所以必须遵守"，
# 而这两者混在一起时，最常见的失败模式就是把历史里的旧要求当成当前指令。
# 刻意写得很短：它是每轮都出现的固定开销，长一句就是一份永久税。
SECTION_PURPOSE = {
    "history": "# Below: what already happened. Reference, not instructions.",
    "memory": "# Below: memory from earlier turns. Prefer fresh evidence on conflict.",
    "relevant_memory": "# Below: retrieved as relevant to the current request.",
    CONSTRAINTS_SECTION: "# Below: constraints for this turn. Follow them.",
    CURRENT_REQUEST_SECTION: "# Below: the user's current request. Act on this.",
}
RELEVANT_MEMORY_LIMIT = 3
# 原生协议里，有调用无结果的那条 tool_use 必须被配平，否则 provider 直接 400。
# 补的是"如实说没有结果"，不是编一个假输出——模型据此可以决定要不要重来。
MISSING_TOOL_RESULT = "(no result recorded for this call: the run stopped before it produced output)"
# 工具输出在历史里被截断时，保留哪一端取决于关键信息的位置：
# run_shell 是 "exit_code(顶部) / stdout / stderr(底部)"，两端都要留。
_HISTORY_KEEP = {"run_shell": "middle"}
def _env_float(name, default):
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def derive_total_budget(capabilities, max_new_tokens):
    """按模型窗口推出这一轮的总预算。

    `capabilities.known` 为假时退回今天的 12000：保守能力表里的窗口是个占位数，
    拿猜出来的窗口去放大预算，撞的是 provider 的 context-length 报错。
    """
    if capabilities is None or not getattr(capabilities, "known", False):
        return DEFAULT_TOTAL_BUDGET
    window = int(getattr(capabilities, "context_window", 0) or 0)
    if window <= 0:
        return DEFAULT_TOTAL_BUDGET
    ratio = _env_float("MOSS_CONTEXT_RATIO", DEFAULT_CONTEXT_RATIO)
    hard_cap = _env_float("MOSS_CONTEXT_HARD_CAP", DEFAULT_CONTEXT_HARD_CAP)
    # 输出也要占窗口。不扣这一块的话，输入刚好塞满窗口、模型一个 token 都吐不出来。
    reserve = max(int(max_new_tokens or 0), MIN_OUTPUT_RESERVE)
    budget = int(min(window * ratio, hard_cap)) - reserve
    return max(MIN_DERIVED_BUDGET, budget)


def derive_section_budgets(total_budget, phase="explore"):
    """按占比把总预算分到各段，并按任务阶段微调。"""
    shares = dict(SECTION_SHARES)
    if phase == "edit":
        # 已经在改文件了：历史里那些"读过什么"的价值下降，
        # 而"改哪个文件、要守什么"直接决定下一步对不对。
        shares["history"] -= PHASE_SHARE_SHIFT
        shares["relevant_memory"] += PHASE_SHARE_SHIFT / 2
        shares[CONSTRAINTS_SECTION] += PHASE_SHARE_SHIFT / 2
    return {section: max(20, int(total_budget * share)) for section, share in shares.items()}


def tool_result_open_tag(item):
    """`<tool_result ...>` 开标签。

    卸载过的输出要在标签上带 `artifact` 和 `lines`：模型看到的是摘要，
    但它得知道完整输出在哪、有多长，否则"可以取回"只是一句空话。
    """
    name = str(item.get("name", "tool"))
    attributes = [
        'untrusted="true"',
        f'source="{name}"',
        f"args={json.dumps(item.get('args', {}), sort_keys=True)}",
    ]
    artifact = str(item.get("artifact", "") or "")
    if artifact:
        attributes.append(f'artifact="{artifact}"')
        attributes.append(f'lines="{int(item.get("artifact_lines", 0) or 0)}"')
    return f"<tool_result {' '.join(attributes)}>"


def _render_memory_note(note):
    trust = str(note.get("trust", "model")).strip() or "model"
    source = str(note.get("source", "")).strip() or "unknown"
    source_label = f" source={source}" if source != "unknown" else ""
    return f"[trust={trust}{source_label}] {note.get('text', '')}"


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


@dataclass(frozen=True)
class ContextBuildResult:
    """一轮 prompt 组装的完整结果，含"能不能发"这个判定。

    为什么不只返回 (prompt, metadata)：超预算过去只标一个 flag 然后照发，
    等于把判断推给 provider——它的反馈是一个 400，而那时候这一轮的钱和时间
    已经花掉了。admission gate 要在本地就说"不发"，并说清为什么。
    """

    request: object
    text: str
    metadata: dict
    sendable: bool = True
    overflow_reason: str | None = None


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=None,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
        measure=None,
    ):
        self.agent = agent
        # measure 决定预算的计量单位：默认 token 估算；测试可注入 len 走字符级。
        self.measure = measure or estimate_tokens
        # 预算按模型窗口推导（spec-06 §4.6）；显式传入时以显式为准。
        capabilities = getattr(getattr(agent, "model_client", None), "capabilities", None)
        self.derived_total_budget = derive_total_budget(
            capabilities, getattr(agent, "max_new_tokens", 4096)
        )
        self.total_budget = int(
            self.derived_total_budget if total_budget is None else total_budget
        )
        self.section_budgets = derive_section_budgets(self.total_budget)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def _clip(self, text, limit, keep="head"):
        """按当前计量单位把 text 截断到 limit 以内。"""
        return clip_to_budget(text, int(limit), measure=self.measure, keep=keep)

    def task_phase(self):
        """探索期还是编辑期。只看"这次运行改过文件没有"，不做更花哨的推断。"""
        events = list(self.agent.stall_events()) if hasattr(self.agent, "stall_events") else []
        return "edit" if any(event.get("workspace_changed") for event in events) else "explore"

    def _phase_adjusted_budgets(self):
        """按任务阶段给这一轮的段预算做微调。

        只是一层临时的加减，不改 `self.section_budgets`——那份是可以被评测和
        调用方覆盖的配置，被每轮悄悄重写的话就没法解释预算到底是谁定的。
        """
        budgets = dict(self.section_budgets)
        if self.task_phase() != "edit":
            return budgets
        shift = int(self.total_budget * PHASE_SHARE_SHIFT)
        history_floor = int(self.section_floors.get("history", 0))
        shift = max(0, min(shift, budgets.get("history", 0) - history_floor))
        if not shift:
            return budgets
        budgets["history"] -= shift
        budgets["relevant_memory"] = budgets.get("relevant_memory", 0) + shift // 2
        budgets[CONSTRAINTS_SECTION] = budgets.get(CONSTRAINTS_SECTION, 0) + (shift - shift // 2)
        return budgets

    def _constraints_text(self):
        """"这一轮必须遵守什么"：checkpoint（含计划）+ 最近的失败。

        为什么单独成段并放在末尾：这些是硬约束，而模型对 prompt 末尾的注意力
        最强；放在稳定前缀尾部时它们既离当前请求最远，又会让稳定头每轮抖动。
        """
        lines = []
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
            if checkpoint_text:
                lines.append(checkpoint_text)
        failures = self._recent_failure_lines()
        if failures:
            lines.append("Recent failures (do not repeat them blindly):")
            lines.extend(failures)
        if hasattr(self.agent, "skill_scope_hint"):
            # scope 命中只加一行提示（spec-09 §9.4）。自动注入全文会把渐进披露
            # 撤销掉：一进某个目录就吃掉几千 token，而模型可能根本不打算用它。
            hint = str(self.agent.skill_scope_hint() or "").strip()
            if hint:
                lines.append(hint)
        return "\n".join(lines)

    def _recent_failure_lines(self, limit=3):
        events = []
        if hasattr(self.agent, "stall_events"):
            events = list(self.agent.stall_events())
        failures = [event for event in events if str(event.get("tool_error_code", "") or "")]
        lines = []
        for event in failures[-limit:]:
            args = event.get("args") or {}
            detail = str(args.get("path", "") or args.get("command", "") or args.get("pattern", ""))
            detail = f" {detail}" if detail else ""
            lines.append(f"- {event.get('name', 'tool')}{detail} -> {event.get('tool_error_code')}")
        return lines

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        相关笔记 + 历史 + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩、以及按 token 估算的规模等信息，后续会进入 trace/report，
          便于解释这轮 prompt 是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `Moss.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "history": "",
            # checkpoint / plan / 最近失败从 prefix 尾部搬到这里：它们每轮都可能变，
            # 挂在稳定前缀后面既打掉 prompt 缓存，又离"当前请求"最远。
            CONSTRAINTS_SECTION: self._constraints_text(),
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }
        selected_notes = []
        if memory_enabled and relevant_memory_enabled and hasattr(self.agent, "memory") and hasattr(self.agent.memory, "retrieval_candidates"):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)
        anchors = []
        if relevant_memory_enabled and hasattr(self.agent, "relevant_file_anchors"):
            anchors = list(self.agent.relevant_file_anchors(user_message))

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(
                section_texts, selected_notes=selected_notes, anchors=anchors
            )
            self._last_rendered = rendered
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                user_message=user_message,
                section_texts=section_texts,
            )
            return prompt, metadata

        budgets = self._phase_adjusted_budgets()
        rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes, anchors=anchors)
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        # 如果 prompt 超预算，就按固定顺序不断压缩。
        # 这里的顺序体现了平台偏好：
        # 先牺牲 relevant_memory，再牺牲 history，然后才动 memory 和 prefix。
        # 最新用户请求永远不裁剪，因为那是本轮最重要的输入。
        #
        # 收缩以「实际渲染规模」为基准而不是「名义预算」：只有当某个 section
        # 真的在占用空间（rendered > floor）时才去削它，并且直接把它的预算压到
        # 「当前渲染规模 - 溢出量」，保证每一步都真正减少内容，不会因为预算高于
        # 实际内容而空转。
        while self.measure(prompt) > self.total_budget:
            overflow = self.measure(prompt) - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                rendered_size = self.measure(rendered[section].rendered)
                if rendered_size <= floor:
                    continue
                new_budget = max(floor, rendered_size - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes, anchors=anchors)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        # 兜底：如果所有可压 section 都到了 floor，prompt 仍然超预算（通常是
        # 当前请求本身巨大，而它按设计永不裁剪），如实标记出来，让上层可见，
        # 而不是悄悄把超大 prompt 发出去。
        over_budget_unrecoverable = self.measure(prompt) > self.total_budget

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=section_texts,
            over_budget_unrecoverable=over_budget_unrecoverable,
        )
        self._last_rendered = rendered
        return prompt, metadata

    def build_result(self, user_message):
        """组 prompt 并给出 admission 判定。

        判定只有两档：`request_too_large`（当前请求自己就装不下，裁剪它违背
        "当前请求永不裁剪"的原则，所以只能拒发）和 `prompt_too_large`
        （所有可压 section 都到 floor 了仍然超）。两者都不调用 provider。

        这道闸不受 feature flag 控制：`context_reduction=off` 只是换掉压缩策略，
        不能把"超预算就别发"这条也一起关掉。
        """
        bundle = self.build_bundle(user_message)
        metadata = dict(bundle.metadata)
        limit = int(self.total_budget)
        prompt_units = int(metadata.get("prompt_measured", 0))
        request_units = int(
            metadata.get("sections", {}).get(CURRENT_REQUEST_SECTION, {}).get("rendered_units", 0)
        )
        unit = "chars" if self.measure is len else "tokens"
        overflow_reason = None
        detail = ""
        if request_units > limit:
            overflow_reason = "request_too_large"
            detail = (
                f"the current request alone is about {request_units} {unit}, "
                f"over the usable context budget of {limit} {unit}"
            )
        elif prompt_units > limit:
            overflow_reason = "prompt_too_large"
            detail = (
                f"the assembled prompt is about {prompt_units} {unit}, "
                f"over the usable context budget of {limit} {unit}"
            )
        metadata["sendable"] = overflow_reason is None
        metadata["overflow_reason"] = overflow_reason
        metadata["overflow_detail"] = detail
        return ContextBuildResult(
            request=bundle.request,
            text=bundle.text,
            metadata=metadata,
            sendable=overflow_reason is None,
            overflow_reason=overflow_reason,
        )

    def build_bundle(self, user_message):
        """把预算后的 sections 放进有信任边界的结构化请求。"""
        _, metadata = self.build(user_message)
        metadata = dict(metadata)
        metadata["context_mode"] = self.agent.context_mode
        rendered = self._last_rendered
        rendered_prefix = rendered["prefix"].rendered
        stable_text, workspace_text = self._split_rendered_prefix(rendered_prefix)
        protocol = self.agent.resolved_tool_protocol()
        native_tools = None
        if protocol == "native":
            native_tools = self.agent.native_tool_definitions()
        system = (
            Block(stable_text, kind="rules", trust="platform", cache=True),
        ) if stable_text else ()
        messages = []
        if workspace_text:
            messages.append(
                Message(
                    role="user",
                    blocks=(Block(workspace_text, kind="workspace", source="workspace", trust="tool"),),
                )
            )
        if self.agent.context_mode == "append_only":
            messages.extend(self._append_only_history_messages(protocol))
        elif protocol == "native":
            messages.extend(self._native_history_messages())
        else:
            history_text = rendered["history"].rendered
            history_trust = "tool" if "<tool_result" in history_text else "model"
            messages.append(
                Message(
                    role="tool" if history_trust == "tool" else "assistant",
                    blocks=(
                        Block(
                            self._with_purpose("history", history_text),
                            kind="history",
                            source="session",
                            trust=history_trust,
                        ),
                    ),
                )
            )
        # 结构化请求里的块顺序必须和 _assemble_prompt 一致：
        # 两边错位的话，"prompt 文本"和"真正发出去的请求"就不是同一份东西了。
        tail_blocks = [
            Block(self._with_purpose("memory", rendered["memory"].rendered), kind="memory", source="memory", trust="model"),
            Block(
                self._with_purpose("relevant_memory", rendered["relevant_memory"].rendered),
                kind="relevant",
                source="retrieval",
                trust="model",
            ),
        ]
        constraints_text = rendered[CONSTRAINTS_SECTION].rendered if CONSTRAINTS_SECTION in rendered else ""
        if constraints_text.strip():
            tail_blocks.append(
                Block(
                    self._with_purpose(CONSTRAINTS_SECTION, constraints_text),
                    kind="constraints",
                    source="runtime",
                    trust="platform",
                )
            )
        tail_blocks.append(
            Block(
                self._with_purpose(CURRENT_REQUEST_SECTION, rendered[CURRENT_REQUEST_SECTION].rendered),
                kind="request",
                trust="user",
            )
        )
        messages.append(Message(role="user", blocks=tuple(tail_blocks)))
        request = ModelRequest(
            system=system,
            messages=tuple(messages),
            tools=tuple(native_tools or ()),
            max_new_tokens=int(getattr(self.agent, "max_new_tokens", 4096)),
            cache_key=getattr(getattr(self.agent, "prefix_state", None), "stable_hash", None),
            protocol=protocol,
        )
        return PromptBundle(request=request, text=request.flatten(), metadata=metadata)

    def _append_only_history_messages(self, protocol):
        history = self._history_entries()
        if protocol == "native":
            messages = self._native_history_messages()
        else:
            messages = [self._append_only_text_message(item) for item in history]

        artifacts = []
        for raw in self.agent.session.get("compaction_artifacts", []):
            try:
                artifact = CompactionArtifact(
                    start=int(raw["start"]),
                    end=int(raw["end"]),
                    summary=str(raw["summary"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= artifact.start < artifact.end <= len(messages) and artifact.summary:
                artifacts.append(artifact)
        for artifact in sorted(artifacts, key=lambda item: item.start, reverse=True):
            replacement = Message(
                role="assistant",
                blocks=(
                    Block(
                        artifact.summary,
                        kind="history",
                        source=f"compaction:{artifact.start}:{artifact.end}",
                        trust="model",
                        cache=True,
                    ),
                ),
            )
            messages[artifact.start:artifact.end] = [replacement]
        return messages

    def _append_only_text_message(self, item):
        role = str(item.get("role", "user"))
        if role == "tool":
            name = str(item.get("name", "tool"))
            text = (
                f'{tool_result_open_tag(item)}\n'
                f'{item.get("content", "")}\n</tool_result>'
            )
            return Message(
                role="tool",
                blocks=(Block(text, kind="tool_result", source=name, trust="tool"),),
                call_id=str(item.get("call_id", "") or "") or None,
            )
        safe_role = role if role in {"user", "assistant"} else "user"
        return Message(
            role=safe_role,
            blocks=(
                Block(
                    f"[{role}] {item.get('content', '')}",
                    kind="history",
                    source="session",
                    trust="user" if safe_role == "user" else "model",
                ),
            ),
        )

    def _native_history_messages(self):
        """把 history 翻成原生工具协议的消息序列。

        原生协议下**一轮的所有 tool_use 必须打成一条 assistant 消息，对应的
        tool_result 必须打成紧随其后的一条 user 消息**——history 里它们是
        「先记两条调用，再记两条结果」的平铺顺序，逐条翻译出来就是
        assistant/assistant/user/user，Anthropic /messages 会直接 400。
        所以这里按"调用组"重组，并保证配平：
        - 调用有记录、结果缺失（步数预算截断、run 中途被打断）→ 补一条说明性的
          tool_result，如实说没有结果，而不是伪造输出，也不是丢掉调用；
        - 结果孤零零存在（对应调用已被裁掉/回退）→ 降级成普通文本消息，
          没有 tool_use 的 tool_result 同样会被 provider 拒收。
        """
        entries = self._history_entries()
        # call_id → 结果在 entries 里的位置。同 id 只认第一条。
        result_positions = {}
        for position, item in enumerate(entries):
            call_id = str(item.get("call_id", "") or "")
            if call_id and str(item.get("role", "")) == "tool" and not item.get("native_tool_call"):
                result_positions.setdefault(call_id, position)

        messages = []
        consumed = set()
        index = 0
        while index < len(entries):
            item = entries[index]
            role = str(item.get("role", "user"))
            call_id = str(item.get("call_id", "") or "")
            if item.get("native_tool_call") and call_id:
                group = []
                while index < len(entries):
                    candidate = entries[index]
                    if not (candidate.get("native_tool_call") and str(candidate.get("call_id", "") or "")):
                        break
                    group.append(candidate)
                    index += 1
                messages.extend(self._native_tool_turn(group, entries, result_positions, consumed))
                continue
            index += 1
            if role == "tool" and call_id:
                if index - 1 in consumed:
                    # 已经跟着它的调用一起发过了。
                    continue
                messages.append(
                    Message(
                        role="user",
                        blocks=(
                            Block(
                                f'{tool_result_open_tag(item)}\n'
                                f'{self._clip(str(item.get("content", "")), 900, keep="head")}\n</tool_result>',
                                kind="history",
                                source=str(item.get("name", "")),
                                trust="tool",
                            ),
                        ),
                    )
                )
                continue
            safe_role = role if role in {"user", "assistant"} else "user"
            trust = "user" if safe_role == "user" else "model"
            messages.append(
                Message(
                    role=safe_role,
                    blocks=(
                        Block(
                            self._clip(str(item.get("content", "")), 900, keep="head"),
                            kind="history",
                            source="session",
                            trust=trust,
                        ),
                    ),
                )
            )
        return messages

    def _native_tool_turn(self, group, entries, result_positions, consumed):
        """一组同轮的原生工具调用 → (assistant 调用消息, tool 结果消息)。"""
        return native_history.native_tool_turn(
            group,
            entries,
            result_positions,
            consumed,
            clip=self._clip,
            missing_result=MISSING_TOOL_RESULT,
        )

    def _with_purpose(self, section, text):
        purpose = SECTION_PURPOSE.get(section, "")
        if not purpose or not str(text).strip():
            return text
        return f"{purpose}\n{text}"

    def _split_rendered_prefix(self, rendered_prefix):
        stable_text = str(getattr(getattr(self.agent, "prefix_state", None), "stable_text", ""))
        if stable_text and rendered_prefix.startswith(stable_text):
            return stable_text, rendered_prefix[len(stable_text):].lstrip()
        marker = "\n\nWorkspace:"
        if marker in rendered_prefix:
            head, tail = rendered_prefix.split(marker, 1)
            return head, "Workspace:" + tail
        return rendered_prefix, ""

    def _render_sections_without_reduction(self, section_texts, selected_notes=None, anchors=()):
        selected_notes = selected_notes or []
        relevant_lines = ["Relevant memory:"]
        if anchors:
            relevant_lines.append(f"- Likely relevant files: {', '.join(anchors)}")
        if selected_notes:
            relevant_lines.extend(f"- {_render_memory_note(note)}" for note in selected_notes)
        else:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history = self._history_entries()
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "memory": SectionRender(raw=section_texts["memory"], budget=len(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details={
                    "selected_notes": [note["text"] for note in selected_notes],
                    "rendered_notes": [_render_memory_note(note) for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw, details={"rendered_entries": []}),
            CONSTRAINTS_SECTION: SectionRender(
                raw=section_texts[CONSTRAINTS_SECTION],
                budget=len(section_texts[CONSTRAINTS_SECTION]),
                rendered=section_texts[CONSTRAINTS_SECTION],
                details={},
            ),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_section_floors(self):
        floors = {
            section: max(20, int(budget) // 4)
            for section, budget in self.section_budgets.items()
        }
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, selected_notes=None, anchors=()):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0), anchors=anchors)
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                raw = section_texts[section]
                # prefix / memory 的信息在开头（规则、任务摘要在前），保头截断。
                rendered_text = self._clip(raw, int(budget), keep="head") if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_relevant_memory(self, selected_notes, budget, anchors=()):
        header = "Relevant memory:"
        # 起点锚和相关记忆同属"与当前请求相关、每轮可能变"的内容，
        # 所以共用一个段和一份预算。锚排在笔记前面：它是这一段里最可执行的一条。
        anchor_line = f"- Likely relevant files: {', '.join(anchors)}" if anchors else ""
        lead = [header] + ([anchor_line] if anchor_line else [])
        note_texts = [
            _render_memory_note(note)
            for note in selected_notes
            if str(note.get("text", "")).strip()
        ]
        raw_lines = lead + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join(lead + ["- none"])
        if budget <= 0:
            # 预算 0 的含义是"这一段这轮不要"，而不是"不限量"。旧代码把
            # `budget <= 0` 当成"跳过预算检查"，结果压得最狠的时候这一段反而全量渲染。
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered="",
                details={
                    "selected_notes": note_texts,
                    "rendered_notes": [],
                    "selected_count": len(note_texts),
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )
        if not note_texts:
            rendered = self._clip(raw, budget, keep="head")
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )

        rendered_notes = []
        for text in note_texts:
            candidate_notes = [*rendered_notes, text]
            candidate = "\n".join(lead + [f"- {item}" for item in candidate_notes])
            if self.measure(candidate) <= budget:
                rendered_notes = candidate_notes
        if rendered_notes:
            rendered = "\n".join(lead + [f"- {text}" for text in rendered_notes])
            per_note_budget = 0
        else:
            per_note_budget = max(1, budget - self.measure("\n".join(lead)) - 3)
            rendered_notes = [self._clip(note_texts[0], per_note_budget, keep="head")]
            rendered = "\n".join(lead + [f"- {rendered_notes[0]}"])
            if self.measure(rendered) > budget:
                rendered = self._clip(raw, budget, keep="head")
                rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        if note_count <= 0:
            return 0
        overhead = self.measure(header) + 3 * note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

    def render_history_text(self):
        """把当前会话历史渲染成一段文本，供 delegate、报告等复用。

        它和 `build()` 走的是同一套历史压缩逻辑（同样的最近窗口、重复读折叠、
        文件摘要复用、旧 shell 摘要），因此不会再出现“两套历史口径对不上”的
        问题——这正是过去 runtime 里另有一份 `history_text` 实现时的隐患。
        """
        budget = int(self.section_budgets.get("history", DEFAULT_SECTION_BUDGETS["history"]))
        return self._render_history_section(budget).rendered

    def _render_history_section(self, budget):
        history = self._history_entries()
        raw = self._raw_history_text(history)
        if not history:
            rendered = "Transcript:\n- empty"
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                },
            )

        # 先试完整保真：预算装得下就一个字都不压。
        # 压缩是**有损**的（旧工具结果折成一行、最近的也砍到 900），而模型看不到
        # 自己刚读到的内容时，唯一能做的就是再读一遍——"读了 25 步没有产出"正是
        # 这么来的。预算没花完却先把内容丢掉，省下来的额度不会有任何人受益。
        full = self._full_fidelity_history(history, budget)
        if full is not None:
            return SectionRender(raw=raw, budget=budget, rendered=full[0], details=full[1])

        # 优先保留最近的历史，因为下一步决策通常最依赖刚刚发生的工具结果。
        recent_window = 6
        recent_start = max(0, len(history) - recent_window)
        history_entries, history_details = self._compressed_history_entries(history, recent_start)
        rendered_entries = []
        for entry in reversed(history_entries):
            recent = bool(entry.get("recent", False))
            candidate_lines = list(entry.get("lines", []))
            candidate_entries = candidate_lines + rendered_entries
            candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
            if self.measure(candidate_rendered) <= budget:
                rendered_entries = candidate_entries
                continue
            if recent:
                available = budget - self.measure("Transcript:")
                if rendered_entries:
                    available -= sum(self.measure(line) + 1 for line in rendered_entries)
                available = max(20, available - 1)
                candidate_lines = [self._clip(line, available, keep="head") for line in candidate_lines]
                candidate_entries = candidate_lines + rendered_entries
                candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
                if self.measure(candidate_rendered) <= budget:
                    rendered_entries = candidate_entries
            else:
                smaller_lines = [self._clip(line, 20, keep="head") for line in candidate_lines]
                smaller_entries = smaller_lines + rendered_entries
                smaller_rendered = "\n".join(["Transcript:", *smaller_entries])
                if self.measure(smaller_rendered) <= budget:
                    rendered_entries = smaller_entries
        rendered = "\n".join(["Transcript:", *rendered_entries])

        # 兜底截断保留结尾：raw 里旧的在前、新的在后，保尾才能留住最近的历史。
        if self.measure(rendered) > budget and budget > 0:
            rendered = self._clip(raw, budget, keep="tail")

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "history_fidelity": "compressed",
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered_entries,
                **history_details,
            },
        )

    def _full_fidelity_history(self, history, budget):
        """整段历史原样渲染；装不下返回 None，交给压缩路径。

        每条仍按 budget 上限裁一次——单条就超过整段预算时（比如一份巨大的
        工具输出）不能让它把后面的历史挤没，那种情况本来就该走压缩路径。
        """
        lines = []
        for item in history:
            lines.extend(self._render_history_item(item, budget))
        rendered = "\n".join(["Transcript:", *lines])
        if self.measure(rendered) > budget:
            return None
        return rendered, {
            "history_fidelity": "full",
            "recent_window": len(history),
            "recent_start": 0,
            "rendered_entries": lines,
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

    def _compressed_history_entries(self, history, recent_start):
        entries = []
        seen_older_reads = set()
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

        for index, item in enumerate(history):
            recent = index >= recent_start
            if recent:
                line_limit = 900
                entries.append(
                    {
                        "recent": True,
                        "lines": self._render_history_item(item, line_limit),
                    }
                )
                continue

            if item["role"] == "tool" and item["name"] == "read_file":
                path = str(item["args"].get("path", "")).strip()
                if path in seen_older_reads:
                    details["collapsed_duplicate_reads"] += 1
                    continue
                seen_older_reads.add(path)
                summary = self._reusable_file_summary(path)
                if summary:
                    entries.append({"recent": False, "lines": [f"{path} -> {summary}"]})
                    details["older_entries_count"] += 1
                    details["reused_file_summary_count"] += 1
                    continue

            if item["role"] == "tool":
                summary_line = self._summarize_old_tool_item(item)
                entries.append({"recent": False, "lines": [summary_line]})
                details["older_entries_count"] += 1
                details["summarized_tool_count"] += 1
                continue

            entries.append({"recent": False, "lines": self._render_history_item(item, 60)})

        return entries, details

    def _reusable_file_summary(self, path):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        snapshot = memory.to_dict()
        summary = snapshot.get("file_summaries", {}).get(str(path), {})
        if not summary:
            return ""
        return str(summary.get("summary", "")).strip()

    def _summarize_old_tool_item(self, item):
        if item["name"] == "run_shell":
            command = str(item["args"].get("command", "")).strip() or "shell"
            return f"{command} -> {self._shell_summary(str(item.get('content', '')))}"
        return self._render_history_item(item, 60)[0]

    def _shell_summary(self, content):
        return historylib.shell_summary(content)

    def _history_entries(self):
        """取进 prompt 的历史条目，跳过 pending 的那些。

        pending 标记的是"本轮 prompt 别处已经渲染过"的内容——目前只有当前
        用户请求（它每轮都作为 `Current user request` 出现）。不跳过的话，
        同一句话会在 Transcript 和 Current user request 里各出现一次。
        """
        history = list(getattr(self.agent, "session", {}).get("history", []))
        return [item for item in history if not item.get("pending")]

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(tool_result_open_tag(item))
                lines.append(str(item["content"]))
                lines.append("</tool_result>")
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            # 工具结果带上不可信标记：它是**数据**，不是指令。
            # prefix 里有一条对应规则说明这一点，两者缺一都不成立
            # （光标注没规则模型不会当回事，光有规则则标不出边界在哪）。
            prefix = tool_result_open_tag(item)
            keep = _HISTORY_KEEP.get(item["name"], "head")
            content = self._clip(item["content"], max(20, line_limit), keep=keep)
            return [prefix, content, "</tool_result>"]
        return [f"[{item['role']}] {self._clip(item['content'], line_limit, keep='head')}"]

    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的（spec-06 §4.5）：稳定规则在前（进缓存段），
        # 历史其次，硬约束靠近末尾，最新请求永远最后。
        # 每段前面挂一句用途说明：历史和约束混在一起时，最常见的失败模式
        # 就是把历史里的旧要求当成当前指令。
        parts = []
        for section in SECTION_ORDER:
            text = rendered[section].rendered if section in rendered else ""
            if not text.strip():
                continue
            purpose = SECTION_PURPOSE.get(section, "")
            parts.append(f"{purpose}\n{text}" if purpose else text)
        return "\n\n".join(parts).strip()

    def context_window(self):
        """模型真实的上下文窗口。拿不到就退回今天的总预算（行为不变）。"""
        capabilities = getattr(getattr(self.agent, "model_client", None), "capabilities", None)
        window = int(getattr(capabilities, "context_window", 0) or 0)
        return window or int(self.total_budget)

    def _context_health(self, prompt, rendered, section_metadata, user_message):
        """上下文健康度（spec-06 §4.5）。

        为什么要量出来：prompt 变差是渐进的——历史越堆越多、相关性越来越低，
        但每一轮看起来都"还行"。只有把占用率、各段占比、无关内容比例、
        历史陈旧度记成时间序列，才能在事后指着某一轮说"从这里开始跑偏"。
        目前只观测不干预（阈值等实测数据再定，见 spec-06 §10 开放问题 2）。
        """
        return healthlib.context_health(
            prompt,
            rendered,
            section_metadata,
            user_message,
            measure=self.measure,
            context_window=self.context_window(),
            history_entries=self._history_entries(),
        )

    def _distractor_ratio(self, rendered, user_message):
        """和当前请求毫无词面关联的内容占了多少 token。

        复用 spec-05 的 BM25 索引：一条历史/笔记如果连一个查询词都碰不到，
        它对这一轮就是纯噪声。这是"注意力被稀释了多少"的一个廉价代理指标。
        """
        return healthlib.distractor_ratio(rendered, user_message, self.measure)

    def _history_staleness(self):
        """最老一条进 prompt 的历史距今多少条。堆得越高，越该考虑压缩。"""
        return historylib.history_staleness(self._history_entries())

    def _metadata(
        self,
        prompt,
        rendered,
        budgets,
        reduction_log,
        selected_notes,
        user_message,
        section_texts,
        over_budget_unrecoverable=False,
    ):
        measure_unit = "chars" if self.measure is len else "tokens"
        section_metadata = {}
        # *_chars 是历史字段名（评测代码在用），但预算的单位其实是 token。
        # 新增同名 *_tokens 并让报告改用后者，旧字段保留一个版本周期。
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
                "rendered_units": self.measure(rendered[section].rendered),
                "raw_tokens": estimate_tokens(rendered[section].raw),
                "budget_tokens": int(budgets.get(section, 0)),
                "rendered_tokens": estimate_tokens(rendered[section].rendered),
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            "rendered_units": self.measure(rendered[CURRENT_REQUEST_SECTION].rendered),
            "raw_tokens": estimate_tokens(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_tokens": None,
            "rendered_tokens": estimate_tokens(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_tokens": estimate_tokens(prompt),
            "measure_unit": measure_unit,
            "prompt_measured": self.measure(prompt),
            "context_health": self._context_health(prompt, rendered, section_metadata, user_message),
            "prompt_budget_chars": self.total_budget,
            "prompt_budget_tokens": self.total_budget,
            "derived_budget_tokens": self.derived_total_budget,
            "task_phase": self.task_phase(),
            "prompt_over_budget": self.measure(prompt) > self.total_budget,
            "over_budget_unrecoverable": bool(over_budget_unrecoverable),
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "episodic")).strip() or "episodic" for note in selected_notes],
                "selected_durable_count": sum(
                    1 for note in selected_notes if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
                ),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
                "retrieval_explain": list(
                    getattr(getattr(self.agent, "memory", None), "last_retrieval_explain", [])
                ),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                # full 表示这一轮历史一个字都没压。压缩是有损的，
                # 报告里要能一眼看出它是被预算逼出来的还是白白发生的。
                "history_fidelity": str(rendered["history"].details.get("history_fidelity", "full")),
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }
