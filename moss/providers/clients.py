"""模型后端适配层。

runtime 只关心一件事：给我一个 prompt，我拿回一段文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from .capabilities import capabilities_for

OPENAI_COMPATIBLE_USER_AGENT = "moss/0.1"


def _message_text(message):
    return "\n\n".join(block.text for block in message.blocks if block.text).strip()


def _openai_structured_input(model_request):
    items = []
    if model_request.system:
        items.append(
            {
                "role": "developer",
                "content": [
                    {"type": "input_text", "text": block.text}
                    for block in model_request.system
                    if block.text
                ],
            }
        )
    for message in model_request.messages:
        if model_request.protocol == "native" and message.blocks:
            block = message.blocks[0]
            if block.kind == "tool_call" and message.call_id:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": message.call_id,
                        "name": block.source,
                        "arguments": block.text,
                    }
                )
                continue
            if block.kind == "tool_result" and message.call_id:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.call_id,
                        "output": block.text,
                    }
                )
                continue
        role = message.role if message.role in {"user", "assistant"} else "user"
        content = [
            {"type": "input_text", "text": block.text}
            for block in message.blocks
            if block.text
        ]
        if content:
            items.append({"role": role, "content": content})
    return items


def _anthropic_structured_messages(model_request):
    messages = []
    for message in model_request.messages:
        if model_request.protocol == "native" and message.blocks:
            block = message.blocks[0]
            if block.kind == "tool_call" and message.call_id:
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": message.call_id,
                                "name": block.source,
                                "input": _parse_native_tool_args(block.text),
                            }
                        ],
                    }
                )
                continue
            if block.kind == "tool_result" and message.call_id:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.call_id,
                                "content": block.text,
                            }
                        ],
                    }
                )
                continue
        role = "assistant" if message.role == "assistant" else "user"
        content = [
            {"type": "text", "text": block.text}
            for block in message.blocks
            if block.text
        ]
        if content:
            messages.append({"role": role, "content": content})
    return messages


def _extract_openai_native_actions(data):
    calls = []
    for item in data.get("output", []):
        if not isinstance(item, dict) or item.get("type") not in {"function_call", "tool_call"}:
            continue
        function = item.get("function", {}) if isinstance(item.get("function"), dict) else {}
        name = item.get("name") or function.get("name")
        arguments = item.get("arguments") if item.get("arguments") is not None else function.get("arguments")
        if name:
            calls.append(
                {
                    "type": "tool",
                    "name": str(name),
                    "args": _parse_native_tool_args(arguments),
                    "call_id": str(item.get("call_id") or item.get("id") or ""),
                }
            )
    if calls:
        return calls
    text = _extract_openai_text(data)
    return [{"type": "final", "text": text}] if text else []


def _extract_anthropic_native_actions(data):
    calls = [
        {
            "type": "tool",
            "name": str(item.get("name", "")),
            "args": _parse_native_tool_args(item.get("input", {})),
            "call_id": str(item.get("id", "") or ""),
        }
        for item in data.get("content", [])
        if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name")
    ]
    if calls:
        return calls
    text = next(
        (
            str(item.get("text"))
            for item in data.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ),
        "",
    )
    return [{"type": "final", "text": text}] if text else []


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.provider = str(getattr(self, "provider", "fake") or "fake")
        self.model = str(getattr(self, "model", "fake") or "fake")
        self.capabilities = getattr(self, "capabilities", capabilities_for(self.provider, self.model))
        self.supports_prompt_cache = False
        self.supports_native_tools = bool(getattr(self, "supports_native_tools", False))
        self.native_tool_format = str(getattr(self, "native_tool_format", "") or "")
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)

    def complete_request(self, request):
        return self.complete(
            request.flatten(),
            request.max_new_tokens,
            prompt_cache_key=request.cache_key,
            prompt_cache_retention="in_memory" if request.cache_key else None,
            tools=request.tools,
        )


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.provider = "ollama"
        self.capabilities = capabilities_for(self.provider, model)
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.supports_native_tools = False
        self.native_tool_format = ""
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        # Ollama 当前不支持我们这里接入的 prompt cache 语义，
        # 所以 runtime 传下来的缓存参数会被忽略。
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        return data.get("response", "")

    def complete_request(self, request):
        return self.complete(request.flatten(), request.max_new_tokens)


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _parse_native_tool_args(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _render_native_tool_call(name, args):
    return "<tool>" + json.dumps(
        {"name": str(name or ""), "args": _parse_native_tool_args(args)},
        separators=(",", ":"),
        sort_keys=True,
    ) + "</tool>"


def _extract_openai_text(data):
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"function_call", "tool_call"}:
            function = item.get("function", {}) if isinstance(item.get("function"), dict) else {}
            name = item.get("name") or function.get("name")
            arguments = item.get("arguments") if item.get("arguments") is not None else function.get("arguments")
            if name:
                return _render_native_tool_call(name, arguments)

    if data.get("output_text"):
        return data["output_text"]

    # 同 `_extract_anthropic_text`：工具调用要先整体扫一遍，
    # 否则夹在前面的开场白文本会把后面的 tool_call 吃掉。
    nested = [
        content
        for item in data.get("output", [])
        for content in item.get("content", [])
        if isinstance(content, dict)
    ]
    for content in nested:
        if content.get("type") in {"function_call", "tool_call"}:
            function = content.get("function", {}) if isinstance(content.get("function"), dict) else {}
            name = content.get("name") or function.get("name")
            arguments = content.get("arguments") if content.get("arguments") is not None else function.get("arguments")
            if name:
                return _render_native_tool_call(name, arguments)
    for content in nested:
        text = content.get("text")
        if text:
            return text

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        for tool_call in message.get("tool_calls", []) or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function", {}) if isinstance(tool_call.get("function"), dict) else {}
            name = tool_call.get("name") or function.get("name")
            arguments = tool_call.get("arguments") if tool_call.get("arguments") is not None else function.get("arguments")
            if name:
                return _render_native_tool_call(name, arguments)
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text

    return ""


def _extract_openai_text_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
        item = event.get("item")
        if isinstance(item, dict):
            text = _extract_openai_text({"output": [item]})
            if text:
                return text
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            text = _extract_openai_text(response)
            if text:
                return text
        text = _extract_openai_text(event)
        if text:
            return text
    if deltas:
        return "".join(deltas)
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response)
    return ""


def _extract_openai_response_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            if event.get("type") == "response.completed":
                text = _extract_openai_text(response)
                if text:
                    return text, response
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text, last_response or {}
        else:
            text = _extract_openai_text(event)
            if text:
                return text, event
    if deltas:
        return "".join(deltas), last_response or {}
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response), last_response
    return "", {}


def parse_openai_usage(data):
    """把 OpenAI usage 归一成 runtime 唯一认的一种缓存统计形状。"""
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    cache_metrics_available = "cached_tokens" in input_details
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cache_read_tokens": cached_tokens,
        "cache_write_tokens": 0,
        "cache_metrics_available": cache_metrics_available,
        # 兼容旧 report/评测字段；新代码统一读 cache_read_tokens。
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


def parse_anthropic_usage(data):
    """解析 Anthropic `/messages` usage，缺缓存字段时明确标成不可用。"""
    usage = data.get("usage") or {}
    cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
    cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    cache_metrics_available = (
        "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
    )
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_metrics_available": cache_metrics_available,
        "cached_tokens": cache_read_tokens,
        "cache_hit": cache_read_tokens > 0,
    }


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout, provider="openai"):
        self.model = model
        self.provider = provider
        self.capabilities = capabilities_for(provider, model)
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = self.capabilities.supports_cache
        self.supports_native_tools = self.capabilities.supports_native_tools
        self.native_tool_format = "openai_responses"
        self.last_completion_metadata = {}

    def complete(
        self,
        prompt,
        max_new_tokens,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        tools=None,
        _model_request=None,
    ):
        """向 OpenAI-compatible `/responses` 接口发起一次模型调用。

        为什么存在：
        runtime 不应该知道 HTTP 细节、SSE 细节、usage 字段长什么样，
        更不应该自己去判断 prompt cache 参数要不要带。这个函数把这些后端
        细节都包起来，对上层暴露统一的 `complete()` 行为。

        输入 / 输出：
        - 输入：完整 prompt、最大输出 token，以及可选的 prompt cache 参数
        - 输出：模型最终文本；同时把 usage / cached_tokens 等元数据写进
          `self.last_completion_metadata`

        在 agent 链路里的位置：
        它位于 `Moss.ask()` 的模型调用阶段，是稳定前缀缓存复用链路真正
        落到 provider API 的地方。
        """
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "input": _openai_structured_input(_model_request) if _model_request is not None else [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_output_tokens": max_new_tokens,
            "stream": False,
            "store": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        # runtime 传入的是“稳定前缀”的签名，而不是整段 prompt 的签名。
        # 这样缓存复用针对的是稳定段，不会因为动态 history 每轮变化而失效。
        if self.supports_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if self.supports_prompt_cache and prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention
        if tools:
            payload["tools"] = list(tools)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": OPENAI_COMPATIBLE_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                    headers = getattr(response, "headers", {}) or {}
                    content_type = headers.get("Content-Type", "")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAI-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        # 有些兼容后端返回普通 JSON，有些返回 SSE。
        # 这里两种都接住，并尽量统一抽取文本和 usage/cache 元数据。
        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:"):
            text, response_data = _extract_openai_response_from_sse(body_text)
            if isinstance(response_data, dict) and response_data:
                # 这些元数据会一路传回 runtime，进入 trace 和 report，
                # 用来观察 prompt cache 是否真的命中。
                self.last_completion_metadata = {
                    "prompt_cache_supported": self.supports_prompt_cache,
                    "prompt_cache_key": prompt_cache_key,
                    "prompt_cache_retention": prompt_cache_retention,
                    **parse_openai_usage(response_data),
                }
            if text:
                if _model_request is not None and _model_request.protocol == "native":
                    return _extract_openai_native_actions(response_data or {"output_text": text})
                return text
            raise RuntimeError("OpenAI-compatible error: could not extract text from event stream response")

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "OpenAI-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {data['error']}")
        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **parse_openai_usage(data),
        }
        if _model_request is not None and _model_request.protocol == "native":
            return _extract_openai_native_actions(data)
        return _extract_openai_text(data)

    def complete_request(self, request):
        return self.complete(
            request.flatten(),
            request.max_new_tokens,
            prompt_cache_key=request.cache_key,
            prompt_cache_retention="in_memory" if request.cache_key else None,
            tools=request.tools,
            _model_request=request,
        )


def _extract_anthropic_text(data):
    # 必须先整体扫一遍 tool_use 再回落到 text：模型常常先吐一句开场白
    # （"I'll explore the project structure..."），再跟上真正的 tool_use，
    # content 顺序就是 ["text", "tool_use"]。若在同一个循环里按顺序返回，
    # 拿到的是那句开场白，output_parser 认不出 <tool> 就当成最终答案，
    # 整个 run 会在第一步工具之后直接收尾（DeepSeek 上必现）。
    blocks = [item for item in data.get("content", []) if isinstance(item, dict)]
    for item in blocks:
        if item.get("type") == "tool_use":
            return _render_native_tool_call(item.get("name"), item.get("input", {}))
    for item in blocks:
        if item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout, provider="anthropic"):
        self.model = model
        self.provider = provider
        self.capabilities = capabilities_for(provider, model)
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = self.capabilities.supports_cache
        self.supports_native_tools = self.capabilities.supports_native_tools
        self.native_tool_format = "anthropic_messages"
        self.last_completion_metadata = {}

    def complete(
        self,
        prompt,
        max_new_tokens,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        tools=None,
        _model_request=None,
    ):
        del prompt_cache_retention
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": _anthropic_structured_messages(_model_request) if _model_request is not None else [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if _model_request is not None and _model_request.system:
            payload["system"] = [
                {"type": "text", "text": block.text}
                for block in _model_request.system
                if block.text
            ]
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
        cache_enabled = bool(
            _model_request is not None
            and prompt_cache_key
            and self.capabilities.supports_cache
            and self.capabilities.cache_style == "anthropic_breakpoint"
        )
        if cache_enabled:
            if payload.get("system"):
                payload["system"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
            if payload.get("tools"):
                payload["tools"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
            if len(payload["messages"]) >= 2 and payload["messages"][-2].get("content"):
                payload["messages"][-2]["content"][-1]["cache_control"] = {"type": "ephemeral"}

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")
        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            **parse_anthropic_usage(data),
        }
        if _model_request is not None and _model_request.protocol == "native":
            native_actions = _extract_anthropic_native_actions(data)
            if native_actions:
                return native_actions
            raise RuntimeError("Anthropic-compatible error: could not extract native output from response")
        text = _extract_anthropic_text(data)
        if text:
            return text
        raise RuntimeError("Anthropic-compatible error: could not extract text from response")

    def complete_request(self, request):
        return self.complete(
            request.flatten(),
            request.max_new_tokens,
            prompt_cache_key=request.cache_key,
            prompt_cache_retention="in_memory" if request.cache_key else None,
            tools=request.tools,
            _model_request=request,
        )
