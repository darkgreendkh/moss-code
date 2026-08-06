"""单文件 trace 可视化（spec-09 §9.6）。

`moss runs show <id> --html > run.html`。纯 stdlib 字符串拼接，
**内联 CSS/SVG，零外部请求**——一份要发给同事看的排查材料，不该在打开时
去连任何东西：既可能连不上（离线排查），更不该把 run_id 泄给第三方 CDN。

页面内容：
- 时间线：每步的 prompt 段落构成（各段 token 占比条）、工具调用与结果摘要、
  耗时、token/成本；
- 失败标签（spec-08 §4.7）高亮；
- 上下文健康度曲线（spec-06 §4.5）用内联 SVG 画。

**脱敏发生在落盘时**（`emit_trace` 里过 `redact_artifact`），这里不做二次处理。
但页眉必须标一句"含脱敏后的工具输出，勿外传"——脱敏是尽力而为，
工具输出里可能有别的敏感东西（内部路径、客户名），它拦不住。
"""

from __future__ import annotations

import html
import json

from . import trace_events

BANNER = (
    "This page contains redacted tool output from a local run. "
    "Redaction is best-effort — review before sharing."
)
# 结果摘要在卡片里的长度上限。完整内容在 trace.jsonl 里，这里只是索引。
RESULT_PREVIEW_CHARS = 600
# SVG 画布。固定尺寸，靠 viewBox 自适应，不引任何图表库。
CHART_WIDTH = 720
CHART_HEIGHT = 160

_STYLE = """
:root { color-scheme: light dark; }
body { margin: 0; font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
       background: #11131a; color: #d7dae0; }
main { max-width: 980px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 20px; margin: 0 0 4px; }
.banner { background: #4a3410; border: 1px solid #8a5f1a; color: #f0d9a8;
          padding: 8px 12px; border-radius: 6px; margin: 12px 0 20px; font-size: 13px; }
.meta { color: #8b93a1; font-size: 13px; margin-bottom: 20px; }
.meta span { margin-right: 16px; }
section { margin: 28px 0; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .08em;
     color: #8b93a1; border-bottom: 1px solid #262b36; padding-bottom: 6px; }
.step { border: 1px solid #262b36; border-radius: 8px; padding: 12px 14px; margin: 12px 0;
        background: #171a22; }
.step.is-error { border-color: #7d2f2f; }
.step-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
.step-name { font-weight: 700; }
.step-cost { color: #8b93a1; font-size: 12px; white-space: nowrap; }
.bar { display: flex; height: 10px; border-radius: 5px; overflow: hidden; margin: 10px 0 6px; }
.bar div { height: 100%; }
.legend { font-size: 12px; color: #8b93a1; }
.legend b { color: #d7dae0; font-weight: 600; }
pre { white-space: pre-wrap; word-break: break-word; margin: 8px 0 0;
      background: #0d0f14; padding: 8px 10px; border-radius: 6px; font-size: 12.5px; }
.tag { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11.5px;
       margin: 2px 4px 2px 0; background: #262b36; }
.tag.is-fail { background: #7d2f2f; color: #ffd7d7; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
td, th { text-align: left; padding: 4px 8px; border-bottom: 1px solid #1e232c; }
th { color: #8b93a1; font-weight: 600; }
@media (prefers-color-scheme: light) {
  body { background: #fbfbfd; color: #1d2027; }
  .step, table { background: #fff; }
  .step { border-color: #e2e5ea; }
  h2 { color: #616a78; border-color: #e2e5ea; }
  .meta, .legend, .step-cost, th { color: #616a78; }
  pre { background: #f2f3f6; }
  .tag { background: #e8eaef; }
  .banner { background: #fff5da; border-color: #e0bd63; color: #6a4c04; }
}
"""

# 各 prompt 段的配色。固定映射，同一段在每一步都是同一个颜色——
# 颜色一抖，"这一步 history 涨了"就看不出来了。
_SECTION_COLORS = {
    "prefix": "#4f7cc4",
    "history": "#c47f4f",
    "memory": "#6aa06a",
    "relevant_memory": "#9a6ac4",
    "constraints": "#c45f7c",
    "current_request": "#4fb0c4",
}
_FALLBACK_COLOR = "#5a6273"


def _esc(value):
    return html.escape(str(value), quote=True)


def _tokens(event):
    metadata = event.get("completion_metadata") or {}
    return metadata.get("input_tokens"), metadata.get("output_tokens")


def _section_shares(prompt_metadata):
    shares = (prompt_metadata.get("context_health") or {}).get("section_share") or {}
    ordered = [(name, float(value)) for name, value in shares.items() if float(value) > 0]
    ordered.sort(key=lambda item: -item[1])
    return ordered


def _bar(shares):
    if not shares:
        return ""
    total = sum(value for _, value in shares) or 1.0
    cells = "".join(
        f'<div style="width:{value / total * 100:.2f}%;background:{_SECTION_COLORS.get(name, _FALLBACK_COLOR)}" '
        f'title="{_esc(name)} {value * 100:.1f}%"></div>'
        for name, value in shares
    )
    legend = " · ".join(f"<b>{_esc(name)}</b> {value * 100:.0f}%" for name, value in shares)
    return f'<div class="bar">{cells}</div><div class="legend">{legend}</div>'


def _health_chart(points):
    """上下文占用率曲线。内联 SVG，没有点就整段省略。"""
    if len(points) < 2:
        return ""
    top = max(max(points), 0.01)
    step = CHART_WIDTH / (len(points) - 1)
    coords = " ".join(
        f"{index * step:.1f},{CHART_HEIGHT - (value / top) * (CHART_HEIGHT - 20) - 10:.1f}"
        for index, value in enumerate(points)
    )
    return (
        f'<svg viewBox="0 0 {CHART_WIDTH} {CHART_HEIGHT}" width="100%" height="{CHART_HEIGHT}" '
        'role="img" aria-label="context utilization over time">'
        f'<polyline points="{coords}" fill="none" stroke="#4f7cc4" stroke-width="2"/>'
        f'<line x1="0" y1="{CHART_HEIGHT - 10}" x2="{CHART_WIDTH}" y2="{CHART_HEIGHT - 10}" '
        'stroke="#3a4150" stroke-width="1"/>'
        f'<text x="4" y="14" fill="#8b93a1" font-size="11">peak {top * 100:.1f}% of the context window</text>'
        "</svg>"
    )


def _failure_tags(events, extra_labels=()):
    """失败标签（spec-08 §4.7）+ 这次 run 里出现过的错误码。"""
    labels = list(extra_labels)
    for event in events:
        code = str(event.get("tool_error_code", "") or "")
        if code and code not in labels:
            labels.append(code)
        if event.get("event") == trace_events.CONTEXT_OVERFLOW and trace_events.CONTEXT_OVERFLOW not in labels:
            labels.append(trace_events.CONTEXT_OVERFLOW)
    return labels


def _step_card(event, prompt_metadata):
    name = str(event.get("event", ""))
    detail = ""
    error = False
    cost = []

    if name == trace_events.PROMPT_BUILT:
        metadata = event.get("prompt_metadata") or {}
        cost.append(f"{metadata.get('prompt_tokens', '?')} prompt tokens")
        detail = _bar(_section_shares(metadata))
    elif name == trace_events.MODEL_PARSED:
        input_tokens, output_tokens = _tokens(event)
        if input_tokens is not None:
            cost.append(f"in {input_tokens}")
        if output_tokens is not None:
            cost.append(f"out {output_tokens}")
        detail = f"<div class=\"legend\">parsed as <b>{_esc(event.get('kind', '?'))}</b></div>"
    elif name == trace_events.TOOL_EXECUTED:
        code = str(event.get("tool_error_code", "") or "")
        error = bool(code)
        args = json.dumps(event.get("args", {}), ensure_ascii=False, sort_keys=True)
        result = str(event.get("result", ""))[:RESULT_PREVIEW_CHARS]
        tags = f'<span class="tag is-fail">{_esc(code)}</span>' if code else ""
        detail = (
            f'<div class="legend"><b>{_esc(event.get("name", "tool"))}</b> {_esc(args)} {tags}</div>'
            f"<pre>{_esc(result)}</pre>"
        )
    elif name in {trace_events.MODEL_ERROR, trace_events.CONTEXT_OVERFLOW, trace_events.BUDGET_EXCEEDED}:
        error = True
        detail = f"<pre>{_esc(json.dumps(event, ensure_ascii=False, sort_keys=True)[:RESULT_PREVIEW_CHARS])}</pre>"
    else:
        # 其余事件只留一行摘要：把每条事件的完整 JSON 都铺开，页面会变成
        # 一份没人读得下去的 jsonl 副本，而那份原文本来就在磁盘上。
        keys = [key for key in sorted(event) if key not in {"event", "created_at", "schema_version", "hash", "prev_hash", "seq"}]
        summary = ", ".join(f"{key}={json.dumps(event[key], ensure_ascii=False)[:80]}" for key in keys[:4])
        detail = f'<div class="legend">{_esc(summary)}</div>' if summary else ""

    duration = event.get("duration_ms") or event.get("run_duration_ms")
    if duration is not None:
        cost.append(f"{int(duration)} ms")
    del prompt_metadata
    return (
        f'<div class="step{" is-error" if error else ""}">'
        f'<div class="step-head"><span class="step-name">{_esc(name)}</span>'
        f'<span class="step-cost">{_esc(" · ".join(cost))}</span></div>'
        f"{detail}</div>"
    )


def _summary_table(rows):
    body = "".join(
        f"<tr><th>{_esc(key)}</th><td>{_esc(value)}</td></tr>" for key, value in rows if value not in (None, "")
    )
    return f"<table>{body}</table>" if body else ""


def render_run_html(run_id, events, report=None, task_state=None):
    """把一次 run 的 trace 渲染成单文件 HTML。

    `report` / `task_state` 可以为 None（归档过的 run 只剩 trace），
    页面照样出得来——排查工具在信息不全时也得能用。
    """
    events = list(events or [])
    report = report or {}
    task_state = task_state or {}

    utilization = [
        float((event.get("prompt_metadata") or {}).get("context_health", {}).get("context_utilization", 0.0))
        for event in events
        if event.get("event") == trace_events.PROMPT_BUILT
    ]
    tags = _failure_tags(events, report.get("failure_labels") or ())
    usage = report.get("usage") or {}
    routing = report.get("model_routing") or {}
    replay = report.get("replay") or {}

    summary = _summary_table(
        [
            ("run_id", run_id),
            ("status", task_state.get("status") or report.get("status")),
            ("stop_reason", task_state.get("stop_reason") or report.get("stop_reason")),
            ("tool_steps", task_state.get("tool_steps")),
            ("model", (report.get("prompt") or {}).get("model")),
            ("provider", (report.get("prompt") or {}).get("provider")),
            ("input tokens", usage.get("input_tokens")),
            ("output tokens", usage.get("output_tokens")),
            ("usd", usage.get("usd")),
            ("aux model", routing.get("aux_model")),
            ("aux degraded", routing.get("aux_degraded")),
            ("replayed from", replay.get("cassette")),
            ("trace events", len(events)),
        ]
    )
    tag_html = "".join(f'<span class="tag is-fail">{_esc(tag)}</span>' for tag in tags) or (
        '<span class="tag">no failure labels</span>'
    )
    chart = _health_chart(utilization)
    steps = "".join(_step_card(event, report) for event in events)

    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>moss run {_esc(run_id)}</title>"
        f"<style>{_STYLE}</style></head><body><main>"
        f"<h1>moss run {_esc(run_id)}</h1>"
        f'<div class="banner">{_esc(BANNER)}</div>'
        f"<section><h2>Summary</h2>{summary}</section>"
        f'<section><h2>Failure labels</h2><div>{tag_html}</div></section>'
        + (f"<section><h2>Context health</h2>{chart}</section>" if chart else "")
        + f"<section><h2>Timeline</h2>{steps}</section>"
        "</main></body></html>\n"
    )
