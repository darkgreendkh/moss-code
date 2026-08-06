"""子 agent 的契约与结果（spec-09 §9.1）。

为什么单独一层：`delegate` 原来把父 history 硬 clip 到 300 字符塞进子 agent 的
notes，再把子 agent 的裸文本拼回父 history。两头都错——

- **入口错**：截断的 history 既不是"必要背景"，也不是"完整背景"。它是一段被
  从中间砍断的对话，子 agent 读到的可能是半句话。而 sub-agent 作为上下文治理
  手段的全部意义，就在于父 agent **显式**决定"这件事需要哪些背景"。
- **出口错**：裸文本拼回父 history，等于把子 agent 烧掉的那几千 token 又原样
  倒回父 context。省下来的上下文一分没省。

所以入口换成 `DelegateContract`（结构化的必要背景），出口换成 `DelegateResult`
（带证据锚点的结论）。父 agent 拿到 finding 之后可以直接 `read_file(path, start, end)`
自己核验，而不是选择相信一段没有出处的话。

**保持只读**。可写子 agent 要等沙箱和动作幂等都到位，否则只是把一个不可恢复的
循环复制多份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 子 agent 允许持有的能力。它必须是父能力的子集，而且只读。
DELEGATE_CAPABILITIES = frozenset({"fs_read"})
# 单次 fan-out 的并发上限。子 agent 各自要跑模型，放开了就是拿钱换延迟。
MAX_FANOUT = 4
# 结构化输出的行首标记。子 agent 不照做也不算失败——解析器会退化成"整段当一条
# 无锚点结论"，只是置信度按无锚点算。
FINDING_PREFIX = "FINDING:"
UNRESOLVED_PREFIX = "UNRESOLVED:"
CONFIDENCE_PREFIX = "CONFIDENCE:"

# `path:12-40` / `path:12` / `path`
_ANCHOR_PATTERN = re.compile(r"\[([^\[\]]+?)(?::(\d+)(?:-(\d+))?)?\]\s*$")


@dataclass(frozen=True)
class Finding:
    """一条结论 + 它的出处。锚点可以为空，但那件事必须看得出来。"""

    claim: str
    evidence_path: str = ""
    line_range: tuple = ()

    @property
    def anchored(self):
        return bool(self.evidence_path)

    def render(self):
        if not self.evidence_path:
            return f"- {self.claim} (no evidence anchor)"
        if len(self.line_range) == 2:
            return f"- {self.claim} [{self.evidence_path}:{self.line_range[0]}-{self.line_range[1]}]"
        return f"- {self.claim} [{self.evidence_path}]"

    def to_dict(self):
        return {
            "claim": self.claim,
            "evidence_path": self.evidence_path,
            "line_range": list(self.line_range),
        }


@dataclass(frozen=True)
class DelegateContract:
    """父 agent 交给子 agent 的全部东西。没写进来的，子 agent 就看不到。"""

    goal: str
    allowed_tools: tuple = ()
    capabilities: frozenset = DELEGATE_CAPABILITIES
    max_steps: int = 3
    max_usd: float | None = None
    # 结构化的必要背景，**不是**截断的 history。
    context_seed: tuple = ()

    def validate_against(self, parent_capabilities):
        """能力必须是父集的子集，而且不能越过只读边界。

        fail-closed：拿不准就拒绝。子 agent 悄悄拿到 fs_write 的后果，
        是一个没人审批过的写入路径。
        """
        parent = frozenset(parent_capabilities or frozenset())
        mine = frozenset(self.capabilities or frozenset())
        escalated = mine - parent
        if escalated:
            raise ValueError(
                f"delegate would escalate capabilities beyond the parent: {sorted(escalated)}"
            )
        writable = mine - DELEGATE_CAPABILITIES
        if writable:
            raise ValueError(f"delegate must stay read-only, got {sorted(writable)}")
        return True

    def seed_text(self):
        """子 agent 看到的背景段。父 agent 显式构造，一个字都不是抄来的。"""
        lines = [
            "You are a bounded read-only investigator spawned by another agent.",
            "Answer with one or more lines in this shape, then stop:",
            f"  {FINDING_PREFIX} <what you found> [path/to/file.py:12-40]",
            f"  {UNRESOLVED_PREFIX} <what you could not answer>",
            f"  {CONFIDENCE_PREFIX} <0.0-1.0>",
            "Every FINDING must cite a file you actually read. Do not guess.",
        ]
        if self.context_seed:
            lines.append("")
            lines.append("Background from the parent agent:")
            lines.extend(f"- {item}" for item in self.context_seed)
        return "\n".join(lines)

    def to_dict(self):
        return {
            "goal": self.goal,
            "allowed_tools": list(self.allowed_tools),
            "capabilities": sorted(self.capabilities),
            "max_steps": int(self.max_steps),
            "max_usd": self.max_usd,
            "context_seed": list(self.context_seed),
        }


@dataclass(frozen=True)
class DelegateResult:
    findings: tuple = ()
    unresolved: tuple = ()
    confidence: float = 0.0
    # tokens / usd / wall_s / steps。进父 run 的总账，子 agent 不是免费的。
    cost: dict = field(default_factory=dict)
    goal: str = ""
    error: str = ""

    def render(self):
        """回给父 agent 的文本。这是**摘要**，不是子 agent 的完整输出。"""
        if self.error:
            return f"delegate_result (goal: {self.goal})\nerror: {self.error}"
        lines = [f"delegate_result (goal: {self.goal}, confidence: {self.confidence:.2f})"]
        lines.extend(finding.render() for finding in self.findings) if self.findings else lines.append(
            "- (no findings)"
        )
        if self.unresolved:
            lines.append("unresolved:")
            lines.extend(f"- {item}" for item in self.unresolved)
        cost = self.cost or {}
        lines.append(
            "cost: "
            + ", ".join(
                f"{key}={cost.get(key)}"
                for key in ("steps", "input_tokens", "output_tokens", "usd", "wall_s")
                if cost.get(key) is not None
            )
        )
        return "\n".join(lines)

    def to_dict(self):
        return {
            "goal": self.goal,
            "findings": [finding.to_dict() for finding in self.findings],
            "unresolved": list(self.unresolved),
            "confidence": self.confidence,
            "cost": dict(self.cost or {}),
            "error": self.error,
        }


def _parse_anchor(text):
    match = _ANCHOR_PATTERN.search(text.strip())
    if match is None:
        return text.strip(), "", ()
    claim = text[: match.start()].strip()
    path = match.group(1).strip()
    if match.group(2) is None:
        return claim, path, ()
    start = int(match.group(2))
    end = int(match.group(3)) if match.group(3) else start
    return claim, path, (start, end)


def parse_delegate_output(text, goal="", verify_anchor=None):
    """把子 agent 的最终答案解析成结构化结果。

    `verify_anchor(path)` 由父 agent 提供：锚点指向的文件不存在（或逃出工作区）
    时把锚点丢掉、只留结论。留一个假锚点比没有锚点更糟——父 agent 会去读，
    读不到，然后不知道该信哪个。

    解析不出任何 FINDING 时**不当失败**：整段答案降级成一条无锚点结论。
    子 agent 没照格式说话是常事，为此丢掉它的工作成果不划算。
    """
    text = str(text or "").strip()
    findings = []
    unresolved = []
    confidence = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(FINDING_PREFIX):
            claim, path, line_range = _parse_anchor(line[len(FINDING_PREFIX):])
            if path and verify_anchor is not None and not verify_anchor(path):
                path, line_range = "", ()
            if claim:
                findings.append(Finding(claim=claim, evidence_path=path, line_range=line_range))
        elif line.startswith(UNRESOLVED_PREFIX):
            item = line[len(UNRESOLVED_PREFIX):].strip()
            if item:
                unresolved.append(item)
        elif line.startswith(CONFIDENCE_PREFIX):
            try:
                confidence = max(0.0, min(1.0, float(line[len(CONFIDENCE_PREFIX):].strip())))
            except ValueError:
                confidence = None

    if not findings and text:
        findings.append(Finding(claim=text))
    if confidence is None:
        # 子 agent 没自报置信度就按锚点比例算：一条有出处的结论比一句断言可信。
        anchored = sum(1 for finding in findings if finding.anchored)
        confidence = (anchored / len(findings)) if findings else 0.0
    return DelegateResult(
        findings=tuple(findings),
        unresolved=tuple(unresolved),
        confidence=float(confidence),
        goal=goal,
    )
