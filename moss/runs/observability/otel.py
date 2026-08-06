"""把 trace 导成 OTLP/JSON。

为什么存在（spec-07 §4.5）：
moss 的 trace 已经是"一次运行的完整时间线"，但它只有 moss 自己认得。
导成 OTLP/JSON 之后，任何 OpenTelemetry 工具链都能直接吃——排查一次跑偏的
运行时，不用再手写脚本去 grep jsonl。

刻意只落文件、不推送 collector：推送要引入网络能力和 SDK 依赖，
而这个仓库的约定是零第三方运行时依赖。推送交给用户自己的工具链。
"""

import hashlib
import json
from datetime import datetime, timezone

SERVICE_NAME = "moss"
SCOPE_NAME = "moss.agent_loop"

# OTLP 的属性值是带类型的 oneof。这里只用到三种，够覆盖 trace 里的全部字段。
_MAX_ATTRIBUTE_CHARS = 4000


def _hex_id(seed, length):
    return hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:length]


def _unix_nano(text):
    try:
        parsed = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


def _attribute(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if value is None:
        return {"key": key, "value": {"stringValue": ""}}
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    # 嵌套结构（prompt_metadata 之类）可能很大。截断是为了让导出的 span
    # 还能被工具链消化；完整内容始终在原始 trace.jsonl 里。
    return {"key": key, "value": {"stringValue": value[:_MAX_ATTRIBUTE_CHARS]}}


# 这些字段已经进了 span 的结构化位置（名字/时间/ID），不必再重复成属性。
_SKIP_ATTRIBUTES = frozenset({"event", "created_at", "event_id", "duration_ms", "run_id"})


def event_to_span(run_id, event, parent_span_id):
    started = _unix_nano(event.get("created_at"))
    duration_ns = int(float(event.get("duration_ms", 0) or 0) * 1_000_000)
    span = {
        "traceId": _hex_id(run_id, 32),
        "spanId": _hex_id(event.get("event_id", f"{run_id}:{event.get('sequence', 0)}"), 16),
        "parentSpanId": parent_span_id,
        "name": str(event.get("event", "event")),
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": str(started),
        "endTimeUnixNano": str(started + duration_ns),
        "attributes": [
            _attribute(key, value)
            for key, value in sorted(event.items())
            if key not in _SKIP_ATTRIBUTES
        ],
        "status": {"code": 2 if _looks_failed(event) else 1},  # ERROR / OK
    }
    return span


def _looks_failed(event):
    if str(event.get("event", "")).endswith("_error"):
        return True
    if str(event.get("tool_status", "")) not in ("", "ok"):
        return True
    return str(event.get("status", "")) == "failed"


def trace_to_otlp(run_id, events):
    """一次 run 的事件列表 -> OTLP/JSON。run 自己是根 span，事件挂在它下面。"""
    events = list(events or [])
    trace_id = _hex_id(run_id, 32)
    root_span_id = _hex_id(f"root:{run_id}", 16)
    starts = [_unix_nano(event.get("created_at")) for event in events]
    starts = [value for value in starts if value]
    root = {
        "traceId": trace_id,
        "spanId": root_span_id,
        "parentSpanId": "",
        "name": str(run_id),
        "kind": 1,
        "startTimeUnixNano": str(min(starts) if starts else 0),
        "endTimeUnixNano": str(max(starts) if starts else 0),
        "attributes": [
            _attribute("moss.run_id", str(run_id)),
            _attribute("moss.event_count", len(events)),
        ],
        "status": {"code": 1},
    }
    spans = [root] + [event_to_span(run_id, event, root_span_id) for event in events]
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attribute("service.name", SERVICE_NAME)]},
                "scopeSpans": [{"scope": {"name": SCOPE_NAME}, "spans": spans}],
            }
        ]
    }
