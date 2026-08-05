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
from dataclasses import dataclass

from .model_request import Block, CompactionArtifact, Message, ModelRequest, PromptBundle
from .token_budget import clip_to_budget, estimate_tokens


# 预算与 floor 以「估算 token」为单位。
DEFAULT_TOTAL_BUDGET = 12000
DEFAULT_SECTION_BUDGETS = {
    "prefix": 3000,
    "memory": 1000,
    "relevant_memory": 800,
    "history": 6000,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 1000,
    "memory": 250,
    "relevant_memory": 200,
    "history": 1500,
}
# 当 prompt 超预算时，会优先压缩这些 section。
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
RELEVANT_MEMORY_LIMIT = 3
# 工具输出在历史里被截断时，保留哪一端取决于关键信息的位置：
# run_shell 是 "exit_code(顶部) / stdout / stderr(底部)"，两端都要留。
_HISTORY_KEEP = {"run_shell": "middle"}
# 判断 shell 输出里“说明成败”的信号行时用到的关键词。
_SHELL_SIGNAL_KEYWORDS = (
    "exit_code",
    "error",
    "err:",
    "fail",
    "failed",
    "failure",
    "traceback",
    "exception",
    "fatal",
    "no such",
    "not found",
    "cannot",
    "denied",
    "assert",
)


def _is_shell_signal_line(line):
    lowered = line.lower()
    return any(keyword in lowered for keyword in _SHELL_SIGNAL_KEYWORDS)


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


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
        measure=None,
    ):
        self.agent = agent
        # measure 决定预算的计量单位：默认 token 估算；测试可注入 len 走字符级。
        self.measure = measure or estimate_tokens
        self.total_budget = int(total_budget)
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def _clip(self, text, limit, keep="head"):
        """按当前计量单位把 text 截断到 limit 以内。"""
        return clip_to_budget(text, int(limit), measure=self.measure, keep=keep)

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
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            section_texts["prefix"] = section_texts["prefix"] + "\n\n" + checkpoint_text
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

        budgets = dict(self.section_budgets)
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
                    blocks=(Block(history_text, kind="history", source="session", trust=history_trust),),
                )
            )
        messages.append(
            Message(
                role="user",
                blocks=(
                    Block(rendered["memory"].rendered, kind="memory", source="memory", trust="model"),
                    Block(
                        rendered["relevant_memory"].rendered,
                        kind="relevant",
                        source="retrieval",
                        trust="model",
                    ),
                    Block(
                        rendered[CURRENT_REQUEST_SECTION].rendered,
                        kind="request",
                        trust="user",
                    ),
                ),
            )
        )
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
            args = json.dumps(item.get("args", {}), sort_keys=True)
            text = (
                f'<tool_result untrusted="true" source="{name}" args={args}>\n'
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
        messages = []
        for item in self._history_entries():
            role = str(item.get("role", "user"))
            call_id = str(item.get("call_id", "") or "") or None
            if item.get("native_tool_call") and call_id:
                messages.append(
                    Message(
                        role="assistant",
                        blocks=(
                            Block(
                                json.dumps(item.get("args", {}), separators=(",", ":"), sort_keys=True),
                                kind="tool_call",
                                source=str(item.get("name", "")),
                                trust="model",
                            ),
                        ),
                        call_id=call_id,
                    )
                )
            elif role == "tool" and call_id:
                messages.append(
                    Message(
                        role="tool",
                        blocks=(
                            Block(
                                self._clip(str(item.get("content", "")), 900, keep="head"),
                                kind="tool_result",
                                source=str(item.get("name", "")),
                                trust="tool",
                            ),
                        ),
                        call_id=call_id,
                    )
                )
            else:
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
            relevant_lines.extend(f"- {note['text']}" for note in selected_notes)
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
                    "rendered_notes": [note["text"] for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw, details={"rendered_entries": []}),
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
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]
        raw_lines = lead + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join(lead + ["- none"])
        if not note_texts:
            rendered = self._clip(raw, budget, keep="head") if budget > 0 else raw
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
            if budget <= 0 or self.measure(candidate) <= budget:
                rendered_notes = candidate_notes
        if rendered_notes:
            rendered = "\n".join(lead + [f"- {text}" for text in rendered_notes])
            per_note_budget = 0
        else:
            per_note_budget = max(1, budget - self.measure("\n".join(lead)) - 3)
            rendered_notes = [self._clip(note_texts[0], per_note_budget, keep="head")]
            rendered = "\n".join(lead + [f"- {rendered_notes[0]}"])
            if self.measure(rendered) > budget and budget > 0:
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
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered_entries,
                **history_details,
            },
        )

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
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            return "(empty)"
        # 优先保留“说明成败”的信号行（exit_code / error / traceback ...）。
        # 常见失败输出里报错在末尾，纯取前 3 行会恰好丢掉最关键的信息。
        signal_lines = [line for line in lines if _is_shell_signal_line(line)]
        chosen = signal_lines[:3] if signal_lines else lines[:3]
        return " | ".join(chosen)

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
                lines.append(
                    f'<tool_result untrusted="true" source="{item["name"]}" args={json.dumps(item["args"], sort_keys=True)}>'
                )
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
            prefix = f'<tool_result untrusted="true" source="{item["name"]}" args={json.dumps(item["args"], sort_keys=True)}>'
            keep = _HISTORY_KEEP.get(item["name"], "head")
            content = self._clip(item["content"], max(20, line_limit), keep=keep)
            return [prefix, content, "</tool_result>"]
        return [f"[{item['role']}] {self._clip(item['content'], line_limit, keep='head')}"]

    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()

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
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
                "rendered_units": self.measure(rendered[section].rendered),
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            "rendered_units": self.measure(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_tokens": estimate_tokens(prompt),
            "measure_unit": measure_unit,
            "prompt_measured": self.measure(prompt),
            "prompt_budget_chars": self.total_budget,
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
