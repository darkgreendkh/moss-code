"""模型输出解析。

为什么存在：
模型输出首先是自然语言文本，而 runtime 需要的是结构化决策：
"这是工具调用"还是"这是最终答案"。如果没有这层解析，后面的工具校验、
审批和执行链路就没法可靠工作。

在 agent 链路里的位置：
位于 `model_client.complete()` 之后、`run_tool()` 之前，是模型输出
进入平台控制流的第一道结构化关口。这里全部是纯函数、不依赖任何
runtime 状态，所以独立成模块，方便单测和复用。
"""

import json
import re


def parse_model_output(raw):
    """把模型原始输出解析成 runtime 可执行的动作或最终答案。

    输入 / 输出：
    - 输入：模型返回的原始文本 `raw`
    - 输出：`(kind, payload)`，其中 `kind` 可能是 `tool`、`final`、`retry`
    """
    raw = str(raw)
    # 这里支持两种工具格式：
    # 1. <tool>...</tool> 里包 JSON，适合简短调用
    # 2. XML 风格属性/子标签，适合写文件这类多行内容
    if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
        body = _extract(raw, "tool")
        try:
            payload = json.loads(body)
        except Exception:
            return "retry", retry_notice("model returned malformed tool JSON")
        if not isinstance(payload, dict):
            return "retry", retry_notice("tool payload must be a JSON object")
        if not str(payload.get("name", "")).strip():
            return "retry", retry_notice("tool payload is missing a tool name")
        args = payload.get("args", {})
        if args is None:
            payload["args"] = {}
        elif not isinstance(args, dict):
            return "retry", retry_notice()
        return "tool", payload
    if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
        payload = parse_xml_tool(raw)
        if payload is not None:
            return "tool", payload
        return "retry", retry_notice()
    if "<final>" in raw:
        final = _extract(raw, "final").strip()
        if final:
            return "final", final
        return "retry", retry_notice("model returned an empty <final> answer")
    raw = raw.strip()
    if raw:
        return "final", raw
    return "retry", retry_notice("model returned an empty response")


def retry_notice(problem=None):
    prefix = "Runtime notice"
    if problem:
        prefix += f": {problem}"
    else:
        prefix += ": model returned malformed tool output"
    return (
        f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. "
        'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
    )


def parse_xml_tool(raw):
    match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
    if not match:
        return None
    attrs = _parse_attrs(match.group("attrs"))
    name = str(attrs.pop("name", "")).strip()
    if not name:
        return None

    body = match.group("body")
    args = dict(attrs)
    for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
        if f"<{key}>" in body:
            args[key] = _extract_raw(body, key)

    body_text = body.strip("\n")
    if name == "write_file" and "content" not in args and body_text:
        args["content"] = body_text
    if name == "delegate" and "task" not in args and body_text:
        args["task"] = body_text.strip()
    return {"name": name, "args": args}


def _parse_attrs(text):
    attrs = {}
    for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
        attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return attrs


def _extract(text, tag):
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start = text.find(start_tag)
    if start == -1:
        return text
    start += len(start_tag)
    end = text.find(end_tag, start)
    if end == -1:
        return text[start:].strip()
    return text[start:end].strip()


def _extract_raw(text, tag):
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start = text.find(start_tag)
    if start == -1:
        return text
    start += len(start_tag)
    end = text.find(end_tag, start)
    if end == -1:
        return text[start:]
    return text[start:end]
