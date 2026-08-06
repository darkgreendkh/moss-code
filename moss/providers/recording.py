"""确定性录制回放（spec-09 §9.8）。

为什么存在：L1 回归原来靠人手写死每一句模型输出（`SCRIPTED_MODEL_OUTPUTS`），
改一次 prompt 就要重写全部脚本，而且那些脚本从来不是模型真正说过的话。
把真实调用录成磁带之后，回归跑的是**真实模型轨迹**，却仍然离线、零成本、确定性。

在链路里的位置：这两个 client 都是**包在真实 client 外面的装饰器**，
`AgentLoop` 只认 `complete()` / `complete_request()` 这一个接口，
所以录制与回放对主循环完全透明。

指纹是这套东西的地基。同一个语义请求在两次运行里必须算出同一把钥匙，
所以规范化要剔除"每次都变但不影响语义"的字段：时间戳、run_id/session_id、
workspace 的绝对路径前缀、耗时。剔得不够 → 全部 miss；剔得太狠 → 两个不同的
请求撞成一把钥匙，回放出错误的回答。这里的取舍是**宁可 miss 也不要撞车**：
miss 有 `on_miss` 策略兜底并且会告警，撞车是静默的错误。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .. import atomic_io
from ..clock import now
from ..execution.safety.secrets import redact_artifact

CASSETTE_MANIFEST = "manifest.json"
CASSETTE_SCHEMA_VERSION = 1
# 未命中时的处理策略。CI 用 fail：磁带过期了就该重录，不该悄悄换一个近似回答。
ON_MISS_CHOICES = ("fail", "nearest", "passthrough")

# 规范化用的替换规则。顺序有意义：先替最长最具体的形状，
# 否则 run_20260806-101112-abc123 会先被时间戳规则啃掉一半。
_VOLATILE_PATTERNS = (
    # run_/task_ id：`run_20260806-101112-a1b2c3`
    (re.compile(r"\b(run|task)_\d{8}-\d{6}-[0-9a-f]{6}\b"), "<id>"),
    # session id：`20260806-101112-a1b2c3`
    (re.compile(r"\b\d{8}-\d{6}-[0-9a-f]{6}\b"), "<id>"),
    # ISO-8601 时间戳
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    # 裸日期。git log 的 "3 days ago" 之类已经是相对量，不用管。
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<date>"),
    # 长十六进制：git sha、内容 digest、artifact 文件名里的 sha12
    (re.compile(r"\b[0-9a-f]{12,64}\b"), "<hex>"),
    # 耗时/毫秒数这类纯统计量出现在工具输出里
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s)\b"), "<dur>"),
)


def _normalize_text(text, root=None):
    """把一段 prompt 文本规范化成"语义相同就逐字节相同"的形状。"""
    text = str(text)
    if root:
        # workspace 段里 cwd / repo_root 是绝对路径，评测每次都在新的临时目录下跑。
        # 不替掉它，同一个任务在两台机器上永远算不出同一把钥匙。
        root_text = str(root)
        text = text.replace(root_text, "<root>")
        # tmp 目录在 macOS 上会经过 /private 前缀的 realpath 转换，两种都替。
        if root_text.startswith("/private"):
            text = text.replace(root_text[len("/private"):], "<root>")
        else:
            text = text.replace("/private" + root_text, "<root>")
    for pattern, replacement in _VOLATILE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def canonical_request(request, root=None):
    """请求的规范化视图。指纹和 `request_digest` 都从这里出发。"""
    return {
        "system": [_normalize_text(block.text, root) for block in request.system if block.text],
        "messages": [
            {
                "role": message.role,
                # call_id 是 provider 每次新生成的随机串，不进指纹。
                "blocks": [_normalize_text(block.text, root) for block in message.blocks if block.text],
            }
            for message in request.messages
        ],
        "tools": [
            json.loads(_normalize_text(json.dumps(tool, sort_keys=True), root))
            for tool in request.tools
        ],
        "max_new_tokens": int(request.max_new_tokens),
        "protocol": str(request.protocol),
    }


def request_fingerprint(request, root=None):
    """规范化后的请求指纹。

    规范化：剔除时间戳、run_id、绝对路径前缀、cwd 这些每次都变但不影响语义的
    字段，再对 (system blocks, messages, tools, max_new_tokens) 做 sha256。
    """
    payload = json.dumps(canonical_request(request, root), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def request_digest(request, root=None):
    """人可读的请求摘要，只用于排查磁带为什么不命中，不参与匹配。"""
    canonical = canonical_request(request, root)
    tail = canonical["messages"][-1]["blocks"] if canonical["messages"] else []
    return {
        "system_blocks": len(canonical["system"]),
        "message_count": len(canonical["messages"]),
        "tool_count": len(canonical["tools"]),
        "protocol": canonical["protocol"],
        "last_message_head": ("\n".join(tail))[:200],
    }


class Cassette:
    """磁带目录的读写。一次调用一个文件，文件名自带序号和指纹前缀。"""

    def __init__(self, directory):
        self.directory = Path(directory)

    def manifest_path(self):
        return self.directory / CASSETTE_MANIFEST

    def read_manifest(self):
        try:
            payload = json.loads(self.manifest_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_manifest(self, payload):
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_io.write_atomic(self.manifest_path(), json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def entry_paths(self):
        if not self.directory.is_dir():
            return []
        return sorted(path for path in self.directory.glob("*.json") if path.name != CASSETTE_MANIFEST)

    def entries(self):
        loaded = []
        for path in self.entry_paths():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("fingerprint"):
                loaded.append(payload)
        return loaded

    def next_sequence(self):
        return len(self.entry_paths())

    def write_entry(self, sequence, payload):
        self.directory.mkdir(parents=True, exist_ok=True)
        name = f"{sequence:03d}-{str(payload['fingerprint'])[:12]}.json"
        atomic_io.write_atomic(
            self.directory / name,
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )
        return name


def _shared_prefix_length(left, right):
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


class _ClientFacade:
    """把被包住的 client 的身份属性透出去。

    为什么必须透传：`AgentLoop` 会读 `provider` / `model` / `capabilities` /
    `supports_native_tools` 来决定协议和缓存，report 也会记 provider+model。
    包一层之后这些属性如果变成 "recording"，回放出来的 run 和真实 run 就不可比了。
    """

    def __init__(self, provider, model, capabilities, supports_prompt_cache, supports_native_tools, native_tool_format):
        self.provider = provider
        self.model = model
        self.capabilities = capabilities
        self.supports_prompt_cache = supports_prompt_cache
        self.supports_native_tools = supports_native_tools
        self.native_tool_format = native_tool_format
        self.last_completion_metadata = {}


class RecordingModelClient(_ClientFacade):
    """包在真实 client 外面，把每次调用落盘。

    落盘前一律过 `redact_artifact`：磁带是要进仓库的，带一次明文 key 就等于
    把它公开了，而且 git 历史里删不掉。
    """

    def __init__(self, inner, cassette_dir, *, root=None, secret_env_names=None, agent_commit="", prompt_version=""):
        super().__init__(
            provider=str(getattr(inner, "provider", "") or ""),
            model=str(getattr(inner, "model", "") or ""),
            capabilities=getattr(inner, "capabilities", None),
            supports_prompt_cache=bool(getattr(inner, "supports_prompt_cache", False)),
            supports_native_tools=bool(getattr(inner, "supports_native_tools", False)),
            native_tool_format=str(getattr(inner, "native_tool_format", "") or ""),
        )
        self.inner = inner
        self.cassette = Cassette(cassette_dir)
        self.root = str(root) if root else ""
        self.secret_env_names = tuple(secret_env_names or ())
        self.cassette.write_manifest(
            {
                "schema_version": CASSETTE_SCHEMA_VERSION,
                "recorded_at": now(),
                "provider": self.provider,
                "model": self.model,
                "agent_commit": str(agent_commit or ""),
                "prompt_version": str(prompt_version or ""),
                "redaction": "responses and request digests pass through redact_artifact before landing on disk",
            }
        )

    def _redact(self, value):
        return redact_artifact(value, secret_env_names=self.secret_env_names)

    def _record(self, request, response):
        fingerprint = request_fingerprint(request, self.root)
        payload = {
            "fingerprint": fingerprint,
            "request_digest": self._redact(request_digest(request, self.root)),
            "response": self._redact(response),
            "usage": self._redact(dict(getattr(self.inner, "last_completion_metadata", {}) or {})),
            "recorded_at": now(),
        }
        self.cassette.write_entry(self.cassette.next_sequence(), payload)
        return payload

    def complete_request(self, request):
        if hasattr(self.inner, "complete_request"):
            response = self.inner.complete_request(request)
        else:
            response = self.inner.complete(request.flatten(), request.max_new_tokens)
        self.last_completion_metadata = dict(getattr(self.inner, "last_completion_metadata", {}) or {})
        self._record(request, response)
        return response

    def complete(self, prompt, max_new_tokens, **kwargs):
        # 兼容仍在走裸 complete() 的调用点。这条路径拿不到结构化请求，
        # 所以指纹退化成对整段 prompt 取值——录出来的磁带仍然可回放。
        from ..context.model_request import Block, Message, ModelRequest

        request = ModelRequest(
            messages=(Message(role="user", blocks=(Block(text=str(prompt), kind="request"),)),),
            max_new_tokens=int(max_new_tokens),
        )
        response = self.inner.complete(prompt, max_new_tokens, **kwargs)
        self.last_completion_metadata = dict(getattr(self.inner, "last_completion_metadata", {}) or {})
        self._record(request, response)
        return response


class ReplayMiss(RuntimeError):
    """磁带里没有这次请求。CI 默认让它炸出来。"""


class ReplayModelClient(_ClientFacade):
    """按请求指纹回放，未命中按策略处理。

    `on_miss`：
    - `fail`（CI 默认）：直接抛 `ReplayMiss`。磁带过期就该重录。
    - `nearest`：按指纹的最长公共前缀取最近邻，stderr 告警 + 记 `replay_miss`。
      只在开发期改 prompt 时有用——它给出的答案未必对得上新请求。
    - `passthrough`：交给 `inner`（真实 client）去跑。没有 inner 时退回 fail。
    """

    def __init__(self, cassette_dir, *, on_miss="fail", inner=None, root=None, miss_observer=None):
        if on_miss not in ON_MISS_CHOICES:
            raise ValueError(f"on_miss must be one of {ON_MISS_CHOICES}")
        cassette = Cassette(cassette_dir)
        manifest = cassette.read_manifest()
        super().__init__(
            provider=str(getattr(inner, "provider", "") or manifest.get("provider", "") or "replay"),
            model=str(getattr(inner, "model", "") or manifest.get("model", "") or "replay"),
            capabilities=getattr(inner, "capabilities", None),
            supports_prompt_cache=bool(getattr(inner, "supports_prompt_cache", False)),
            supports_native_tools=bool(getattr(inner, "supports_native_tools", False)),
            native_tool_format=str(getattr(inner, "native_tool_format", "") or ""),
        )
        self.cassette = cassette
        self.manifest = manifest
        self.on_miss = on_miss
        self.inner = inner
        self.root = str(root) if root else ""
        # 未命中的回调。runtime 挂上去之后 miss 会进 trace，不只是 stderr 一行。
        self.miss_observer = miss_observer
        self.misses = []
        entries = cassette.entries()
        # 同一把钥匙可能录到多条（同一个 prompt 被问了两次）。按录制顺序排队，
        # 逐次弹出——否则一个"重复读同一个文件"的轨迹会永远回放第一次的回答，
        # 主循环的重复检测就再也走不到了。
        self._queues = {}
        for entry in entries:
            self._queues.setdefault(entry["fingerprint"], []).append(entry)
        self._cursor = {key: 0 for key in self._queues}

    def __len__(self):
        return sum(len(queue) for queue in self._queues.values())

    def _take(self, fingerprint):
        queue = self._queues.get(fingerprint)
        if not queue:
            return None
        index = self._cursor.get(fingerprint, 0)
        # 用完之后停在最后一条上：同一个请求被问第三次时回放最后一次的回答，
        # 比直接 miss 更接近"模型还是那么答"。
        entry = queue[min(index, len(queue) - 1)]
        self._cursor[fingerprint] = index + 1
        return entry

    def _nearest(self, fingerprint):
        best = None
        best_score = -1
        for key, queue in sorted(self._queues.items()):
            score = _shared_prefix_length(key, fingerprint)
            if score > best_score:
                best, best_score = queue[0], score
        return best

    def _on_miss(self, request, fingerprint):
        digest = request_digest(request, self.root)
        record = {"fingerprint": fingerprint, "on_miss": self.on_miss, **digest}
        self.misses.append(record)
        observer = self.miss_observer
        if observer is not None:
            try:
                observer(record)
            except Exception:
                # 观察者只负责记账，不能把回放本身弄崩。
                pass
        if self.on_miss == "passthrough" and self.inner is not None:
            response = self.inner.complete_request(request)
            self.last_completion_metadata = dict(getattr(self.inner, "last_completion_metadata", {}) or {})
            return response
        if self.on_miss == "nearest":
            entry = self._nearest(fingerprint)
            if entry is not None:
                self.last_completion_metadata = dict(entry.get("usage", {}) or {})
                return entry.get("response")
        raise ReplayMiss(
            f"cassette {self.cassette.directory} has no entry for fingerprint {fingerprint[:12]} "
            f"({digest['message_count']} message(s), protocol={digest['protocol']}); "
            "re-record the cassette or run with on_miss=nearest"
        )

    def complete_request(self, request):
        fingerprint = request_fingerprint(request, self.root)
        entry = self._take(fingerprint)
        if entry is None:
            return self._on_miss(request, fingerprint)
        self.last_completion_metadata = dict(entry.get("usage", {}) or {})
        return entry.get("response")

    def complete(self, prompt, max_new_tokens, **kwargs):
        from ..context.model_request import Block, Message, ModelRequest

        request = ModelRequest(
            messages=(Message(role="user", blocks=(Block(text=str(prompt), kind="request"),)),),
            max_new_tokens=int(max_new_tokens),
        )
        return self.complete_request(request)
