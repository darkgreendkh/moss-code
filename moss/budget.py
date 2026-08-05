"""一次运行的多维预算。

为什么存在：`max_steps` 只约束"做了多少步"，约束不了"花了多少钱、跑了多久"。
一个 25 步的预算，每步可能是 200 token，也可能是 20 万 token；模型调用一次
可能 200ms，也可能因为后端重试卡 5 分钟。真实世界里让人心慌的是后两者。

软/硬两级：软阈值（80%）只提醒模型开始收敛，硬阈值不再调用模型、
直接走优雅收尾——因为这时候再发一次请求，很可能正好把预算捅穿。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 达到限额的这个比例就发软告警。留 20% 的余量给"收敛"这件事本身。
SOFT_RATIO = 0.8

# 各维度的对外名字，也是 trace 里 dimension 字段的取值。
DIMENSIONS = ("steps", "input_tokens", "output_tokens", "wall_clock_s", "usd")


@dataclass
class RunBudget:
    max_steps: int = 0
    max_input_tokens: int = None
    max_output_tokens: int = None
    max_wall_clock_s: float = None
    max_usd: float = None

    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_s: float = 0.0
    # None 表示"不知道价格"，和 0.0（免费）语义不同，绝不能合并：
    # 把未知当成 0 会让金额上限永远不触发，正好在最该拦的时候不拦。
    usd: float = None
    # usage 是估算的（后端没返回真实 usage）。进 report 供评测口径识别。
    usage_estimated: bool = False
    _soft_reported: set = field(default_factory=set)

    def consume(self, *, steps=0, input_tokens=0, output_tokens=0, elapsed_s=0.0, usd=None, estimated=False):
        self.steps += int(steps)
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)
        if elapsed_s:
            # elapsed 是"从 run 开始到现在"的累计值，不是增量。
            self.elapsed_s = float(elapsed_s)
        if usd is not None:
            self.usd = (self.usd or 0.0) + float(usd)
        if estimated:
            self.usage_estimated = True
        return self

    def _pairs(self):
        return (
            ("steps", self.steps, self.max_steps),
            ("input_tokens", self.input_tokens, self.max_input_tokens),
            ("output_tokens", self.output_tokens, self.max_output_tokens),
            ("wall_clock_s", self.elapsed_s, self.max_wall_clock_s),
            ("usd", self.usd, self.max_usd),
        )

    def hard_exceeded(self):
        """返回第一个超限的维度名，没有就返回 None。"""
        for name, used, limit in self._pairs():
            if not limit or used is None:
                continue
            if used >= limit:
                return name
        return None

    def soft_exceeded(self):
        """返回第一个达到 80%、且还没报过的维度名。

        报过就不再报：软告警的作用是"提醒一次收敛"，
        每轮都喊等于把剩下的预算花在喊话上。
        """
        for name, used, limit in self._pairs():
            if not limit or used is None or name in self._soft_reported:
                continue
            if used >= limit * SOFT_RATIO:
                self._soft_reported.add(name)
                return name
        return None

    def snapshot(self):
        """进 report 的 usage 段。"""
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "input_tokens": self.input_tokens,
            "max_input_tokens": self.max_input_tokens,
            "output_tokens": self.output_tokens,
            "max_output_tokens": self.max_output_tokens,
            "wall_clock_s": round(self.elapsed_s, 3),
            "max_wall_clock_s": self.max_wall_clock_s,
            "usd": self.usd,
            "max_usd": self.max_usd,
            "usage_estimated": self.usage_estimated,
        }

    def soft_notice(self, dimension):
        return (
            f"Runtime notice: this run has used most of its {dimension} budget. "
            "Wrap up what you can verify now and return a <final> answer describing what is left."
        )


def usage_from_metadata(metadata, prompt="", completion="", measure=None):
    """从后端返回的 usage 里取 token 数；拿不到就用估算兜底。

    返回 (input_tokens, output_tokens, estimated)。估算标记必须传出去——
    report 里分不清"真实 usage"和"估出来的"，成本口径就没法信。
    """
    metadata = dict(metadata or {})
    input_tokens = metadata.get("input_tokens")
    output_tokens = metadata.get("output_tokens")
    if input_tokens is not None and output_tokens is not None:
        return int(input_tokens), int(output_tokens), False
    measure = measure or (lambda text: 0)
    return int(measure(prompt)), int(measure(completion)), True


def graceful_final(task_state, dimension):
    """硬超限时用规则生成的收尾答案。

    刻意不花一次模型调用来写总结：预算已经用光了，再发一次请求正好把它捅穿。
    """
    lines = [
        f"Stopped: the {dimension} budget for this run is exhausted.",
        f"Completed: {task_state.tool_steps} tool step(s) across {task_state.model_turns} model turn(s).",
    ]
    if task_state.last_tool:
        lines.append(f"Last action: {task_state.last_tool}.")
    lines.append("Suggested next step: rerun with a larger budget, or narrow the request.")
    return "\n".join(lines)
