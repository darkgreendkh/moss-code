"""Explicit provider/model capability table with conservative fallback."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    supports_native_tools: bool
    supports_cache: bool
    cache_style: str
    reports_cache_usage: bool
    max_output_tokens: int


CONSERVATIVE_CAPABILITIES = ModelCapabilities(
    context_window=32000,
    supports_native_tools=False,
    supports_cache=False,
    cache_style="none",
    reports_cache_usage=False,
    max_output_tokens=4096,
)


PROVIDER_CAPABILITIES = {
    ("openai", "gpt-"): ModelCapabilities(128000, True, True, "openai_prefix", True, 32768),
    ("anthropic", "claude-"): ModelCapabilities(
        200000, True, True, "anthropic_breakpoint", True, 64000
    ),
    ("deepseek", "deepseek-"): ModelCapabilities(
        128000, True, True, "anthropic_breakpoint", True, 8192
    ),
    ("ollama", ""): ModelCapabilities(32000, False, False, "none", False, 4096),
}


def capabilities_for(provider, model):
    provider = str(provider or "").lower()
    model = str(model or "").lower()
    matches = [
        (prefix, capabilities)
        for (candidate_provider, prefix), capabilities in PROVIDER_CAPABILITIES.items()
        if candidate_provider == provider and model.startswith(prefix)
    ]
    if not matches:
        return CONSERVATIVE_CAPABILITIES
    return max(matches, key=lambda item: len(item[0]))[1]


def probe(client, first_response_usage):
    """缓存 usage 字段是后端自证；只允许从保守默认升级，不猜 URL。"""
    current = getattr(client, "capabilities", CONSERVATIVE_CAPABILITIES)
    usage = dict(first_response_usage or {})
    if current.supports_cache or not usage.get("cache_metrics_available", False):
        return current
    native_format = str(getattr(client, "native_tool_format", "") or "")
    cache_style = "anthropic_breakpoint" if native_format == "anthropic_messages" else "openai_prefix"
    return replace(
        current,
        supports_cache=True,
        cache_style=cache_style,
        reports_cache_usage=True,
    )
