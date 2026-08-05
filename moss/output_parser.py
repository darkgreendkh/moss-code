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
from dataclasses import dataclass

# 一次输出里可能有多个动作块。按出现位置扫描，顺序即执行顺序。
_BLOCK_RE = re.compile(
    r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>|<final>(?P<final>.*?)</final>",
    re.S,
)


@dataclass(frozen=True)
class Action:
    """模型这一轮想做的一件事。

    index 是它在本轮输出中的出现次序，**执行顺序和写回 history/trace 的顺序
    都按它来**，不是按完成顺序——否则并行执行之后同一批动作重放两次会得到
    不同的记录，录制回放就不成立了。
    """

    kind: str            # "tool" | "final" | "retry"
    index: int = 0
    name: str = ""
    args: dict = None
    text: str = ""
    call_id: str = ""    # 原生协议的调用 ID，回传结果时原样带回


def parse_model_actions(raw, *, protocol="text"):
    """把一轮模型输出解析成有序的动作列表。

    单动作时**逐字节退化成 parse_model_output 的结果**：这条兼容约束是硬的，
    现有解析测试一行不改就必须通过，所以这里直接委托过去，而不是另写一套。
    """
    raw = str(raw)
    matches = list(_BLOCK_RE.finditer(raw))
    if len(matches) <= 1:
        kind, payload = parse_model_output(raw)
        return [_single_action(kind, payload)]

    actions = []
    for index, match in enumerate(matches):
        if match.group("final") is not None:
            text = match.group("final").strip()
            if text:
                actions.append(Action(kind="final", index=index, text=text))
            else:
                actions.append(
                    Action(
                        kind="retry",
                        index=index,
                        text=retry_notice("model returned an empty <final> answer"),
                    )
                )
            continue
        actions.append(_tool_action(match, index))
    return actions


def _single_action(kind, payload):
    if kind == "tool":
        return Action(
            kind="tool",
            index=0,
            name=str(payload.get("name", "")),
            args=payload.get("args", {}),
            call_id=str(payload.get("call_id", "") or ""),
        )
    return Action(kind=kind, index=0, text=str(payload))


def _tool_action(match, index):
    attrs_text = match.group("attrs") or ""
    body = match.group("body") or ""
    if not attrs_text.strip():
        # <tool>{...}</tool>：JSON 形式
        try:
            payload = json.loads(body.strip())
        except Exception:
            return Action(kind="retry", index=index, text=retry_notice("model returned malformed tool JSON"))
        if not isinstance(payload, dict):
            return Action(kind="retry", index=index, text=retry_notice("tool payload must be a JSON object"))
        name = str(payload.get("name", "")).strip()
        if not name:
            return Action(kind="retry", index=index, text=retry_notice("tool payload is missing a tool name"))
        args = payload.get("args", {})
        if args is None:
            args = {}
        elif not isinstance(args, dict):
            return Action(kind="retry", index=index, text=retry_notice())
        return Action(
            kind="tool",
            index=index,
            name=name,
            args=args,
            call_id=str(payload.get("call_id", "") or ""),
        )
    payload = parse_xml_tool(match.group(0))
    if payload is None:
        return Action(kind="retry", index=index, text=retry_notice())
    args = dict(payload.get("args", {}))
    # call_id 从 args 里摘出来：它是协议字段，不是工具参数，
    # 混在 args 里会让工具校验因为"多了一个未知字段"而失败。
    call_id = str(args.pop("call_id", "") or "")
    return Action(kind="tool", index=index, name=payload["name"], args=args, call_id=call_id)


def truncate_after_final(actions):
    """final 之后的动作全部丢弃，返回 (保留的, 丢弃的)。

    模型在给出最终答案之后还接着调工具，说明它自己没想清楚。
    执行这些动作只会让状态和它宣称的结论对不上，所以一律不执行、只记一笔。
    """
    kept = []
    for action in actions:
        kept.append(action)
        if action.kind == "final":
            break
    return kept, actions[len(kept) :]


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
