"""带日期与来源的模型价格表，以及 L2 trial 的成本口径。"""

from dataclasses import asdict, dataclass


PRICE_TABLE_DATE = "2026-08-06"


@dataclass(frozen=True)
class Price:
    """每百万 token 的标准美元价格。"""

    input: float
    output: float
    cache_read: float
    cache_write: float
    source: str


_OPENAI_GPT_55 = Price(
    input=5.0,
    output=30.0,
    cache_read=0.5,
    cache_write=5.0,
    source="https://developers.openai.com/api/docs/models/gpt-5.5",
)

PRICE_TABLE = {
    # gpt-5-5 是本仓库历史配置名；与官方 gpt-5.5 共用同一张可审计价目表。
    ("openai", "gpt-5-5"): _OPENAI_GPT_55,
    ("openai", "gpt-5.5"): _OPENAI_GPT_55,
    ("anthropic", "claude-opus-5"): Price(
        input=5.0,
        output=25.0,
        cache_read=0.5,
        # Moss 的 Anthropic breakpoint 固定使用 1h TTL。
        cache_write=10.0,
        source="https://platform.claude.com/docs/en/about-claude/pricing",
    ),
    ("deepseek", "deepseek-v4-pro"): Price(
        input=0.435,
        output=0.87,
        cache_read=0.003625,
        cache_write=0.435,
        source="https://api-docs.deepseek.com/quick_start/pricing",
    ),
}


def estimate_cost(provider, model, usage):
    provider = str(provider or "").lower()
    model = str(model or "").lower()
    price = PRICE_TABLE.get((provider, model))
    if price is None:
        return None
    usage = dict(usage or {})
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_read_tokens = int(usage.get("cache_read_tokens", 0) or 0)
    cache_write_tokens = int(usage.get("cache_write_tokens", 0) or 0)
    if min(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens) < 0:
        raise ValueError("token usage cannot be negative")
    # OpenAI 的 input_tokens 包含 cached input；Anthropic-compatible usage 则把
    # cache read/write 独立于 input_tokens 返回。统一成四个互斥计费桶。
    uncached_input = max(0, input_tokens - cache_read_tokens - cache_write_tokens)
    if provider in {"anthropic", "deepseek"}:
        uncached_input = input_tokens
    total_per_million = (
        uncached_input * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read
        + cache_write_tokens * price.cache_write
    )
    return total_per_million / 1_000_000.0


@dataclass(frozen=True)
class TrialMetrics:
    passed: bool
    usd: float | None
    wall_s: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    model_turns: int
    tool_calls: int

    @classmethod
    def from_mapping(cls, value):
        value = dict(value or {})
        usd = value.get("usd")
        return cls(
            passed=bool(value.get("passed", False)),
            usd=None if usd is None else float(usd),
            wall_s=float(value.get("wall_s", 0.0) or 0.0),
            input_tokens=int(value.get("input_tokens", 0) or 0),
            output_tokens=int(value.get("output_tokens", 0) or 0),
            cache_read_tokens=int(value.get("cache_read_tokens", 0) or 0),
            cache_write_tokens=int(value.get("cache_write_tokens", 0) or 0),
            model_turns=int(value.get("model_turns", 0) or 0),
            tool_calls=int(value.get("tool_calls", 0) or 0),
        )

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def to_dict(self):
        return asdict(self)


def equal_budget_summary(rows, *, budget_usd=None, budget_tokens=None):
    rows = list(rows)
    eligible = []
    for row in rows:
        trial = TrialMetrics.from_mapping(row)
        if budget_usd is not None and (trial.usd is None or trial.usd > float(budget_usd)):
            continue
        if budget_tokens is not None and trial.total_tokens > int(budget_tokens):
            continue
        eligible.append(trial)
    completed = sum(1 for trial in eligible if trial.passed)
    return {
        "n": len(rows),
        "within_budget": len(eligible),
        "completed": completed,
        "completion_rate": completed / len(eligible) if eligible else 0.0,
    }


def pareto_frontier(rows):
    rows = [dict(row) for row in rows if row.get("usd") is not None]

    def dominates(left, right):
        left_axes = (int(bool(left.get("passed"))), -float(left["usd"]), -float(left.get("wall_s", 0.0)))
        right_axes = (int(bool(right.get("passed"))), -float(right["usd"]), -float(right.get("wall_s", 0.0)))
        return all(a >= b for a, b in zip(left_axes, right_axes)) and any(
            a > b for a, b in zip(left_axes, right_axes)
        )

    return [row for row in rows if not any(other is not row and dominates(other, row) for other in rows)]


def render_pareto(rows):
    lines = ["Pareto:"]
    for row in pareto_frontier(rows):
        label = str(row.get("task_id") or row.get("variant") or "trial")
        lines.append(
            f"- {label}: pass={int(bool(row.get('passed')))} "
            f"usd=${float(row['usd']):.6f} wall_s={float(row.get('wall_s', 0.0)):.3f}"
        )
    return "\n".join(lines)
