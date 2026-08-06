# Spec 04 — 提示词拼接与缓存复用

| 项 | 值 |
| --- | --- |
| 状态 | Draft |
| 对应优化章节 | [第 4 章](../plans/archive/2026-agent-upgrade-plan.md)（4.1–4.6） |
| 优先级 | 4.1 / 4.2 / 4.3 / 4.5 是 P0；4.4 是 P1；4.6 是 P2 |
| 依赖 | 无（角色分层与 [spec-03](spec-03-tool-safety.md) §4.4 是同一次改造） |
| 被依赖 | [spec-02](spec-02-agent-loop.md)（`call_id`）、[spec-06](spec-06-context.md)（结构化 sections）、[spec-08](spec-08-evaluation.md)（缓存与成本指标） |

## 1. 背景与问题

整个 prompt 拼成一个字符串，作为**单条 user message** 发出。后果有三层：

1. **成本**：默认 provider 是 DeepSeek（走 Anthropic `/messages`），`supports_prompt_cache = False`，缓存链路一次都没生效过；即使生效，每轮重渲染历史也会让前缀失配。
2. **安全**：仓库文本与用户指令在模型眼里同权威（这是 [spec-03](spec-03-tool-safety.md) 注入问题的根源）。
3. **可信度**：Anthropic 路径 `last_completion_metadata` 恒为空 dict，于是 `cache_hit_rate` 永远是 0——**却被当作结论在报告里用**。

外加一个空转的开关：`DEFAULT_FEATURE_FLAGS` 里的 `prompt_cache` 从来没有被读过。

## 2. 目标 / 非目标

**目标**

1. `complete()` 从"一个字符串"升级为"system blocks + messages"，角色分层。
2. Anthropic 路径接通 `cache_control`（含 1h TTL）与 usage 解析；OpenAI 路径保留 `prompt_cache_key` 并保证前缀稳定。
3. 历史布局改成 append-only，压缩只在显式 compaction 点发生。
4. run 内冻结 tools/skills，保证 `prompt_cache_key` 恒定。
5. native tool use 与文本协议二选一，`call_id` 全程保留。
6. 缓存指标要么是真值，要么显式 `not available`——**不再出现伪 0**。

**非目标**

- 不做 provider 有状态续接（`previous_response_id` 一类）；本地 session 仍是唯一恢复事实源。
- 不做 tokenizer 精确计数（见 [spec-06](spec-06-context.md) §4.4 的在线校准）。
- 不实现渐进工具发现（工具只有 8 个，等 [spec-09](spec-09-new-modules.md) 接 MCP 后再说）。

## 3. 现状（代码事实）

| 事实 | 位置 |
| --- | --- |
| OpenAI 路径：单条 `input_text` 的 user message | [moss/providers/clients.py:313](moss/providers/clients.py#L313) |
| `supports_prompt_cache = "openai.com" in self.base_url` | [moss/providers/clients.py:290](moss/providers/clients.py#L290) |
| Anthropic 路径：`supports_prompt_cache = False`；`del prompt_cache_key, prompt_cache_retention` | [moss/providers/clients.py:437](moss/providers/clients.py#L437)、[moss/providers/clients.py:445](moss/providers/clients.py#L445) |
| Anthropic `last_completion_metadata` 恒为空 dict | [moss/providers/clients.py:446](moss/providers/clients.py#L446) |
| 原生 tool_use 序列化回 `<tool>` 文本，只取第一个 | [moss/providers/clients.py:107](moss/providers/clients.py#L107) |
| 主循环只看 `model_client.supports_prompt_cache`，不读 feature flag | [moss/agent_loop.py:110](moss/agent_loop.py#L110) |
| `DEFAULT_FEATURE_FLAGS["prompt_cache"] = True` 无人读取 | [moss/runtime.py:42](moss/runtime.py#L42) |
| `refresh_prefix` 每轮重建 skills/tools | [moss/runtime.py:244](moss/runtime.py#L244) |
| `stable_hash` 只覆盖身份/规则/Tools/Skills 段（设计正确） | [moss/prompt_prefix.py:11](moss/prompt_prefix.py#L11) |
| 文本协议规则与示例硬编码在 prefix | [moss/prompt_prefix.py:105](moss/prompt_prefix.py#L105) |
| `_render_history_section` 每轮重渲染并改写旧条目 | [moss/context_manager.py:419](moss/context_manager.py#L419) |

## 4. 设计

### 4.1 结构化请求

```python
# moss/model_request.py
@dataclass(frozen=True)
class Block:
    text: str
    kind: str            # identity | rules | tools | skills | workspace | memory
                         # | relevant | history | request | tool_result
    source: str = ""     # 例如 read_file:docs/x.md
    trust: str = "platform"   # platform | user | model | tool
    cache: bool = False       # 是否在此块之后打缓存断点

@dataclass(frozen=True)
class Message:
    role: str            # user | assistant | tool
    blocks: tuple[Block, ...]
    call_id: str | None = None

@dataclass(frozen=True)
class ModelRequest:
    system: tuple[Block, ...]
    messages: tuple[Message, ...]
    tools: tuple[dict, ...] = ()
    max_new_tokens: int = 4096
    cache_key: str | None = None
    protocol: str = "text"      # text | native

    def flatten(self) -> str:
        """兼容路径：拍平成今天那种单字符串 prompt。

        为什么存在：Ollama 的 /api/generate 只吃单串；
        以及所有断言 prompt 文本的现有测试都要靠它继续通过。
        """
```

**角色分配**

| 内容 | 去处 | trust |
| --- | --- | --- |
| 身份 / 规则 / 工具协议 / Skills 目录 | `system`（Anthropic）或首条 `developer`（OpenAI） | `platform` |
| 用户请求 | 最后一条 `user` message | `user` |
| workspace / repo map / memory / relevant | `user` message 里的独立 block，带 `source` | `tool`/`model` |
| 工具结果 | 独立 message，包在 `<tool_result untrusted="true" source="...">` | `tool` |

**不变量**：仓库内容与工具输出**永不进 system**。这条要有测试守住（[spec-03](spec-03-tool-safety.md) 的注入测试会引用它）。

### 4.2 append-only 历史

- `ContextManager.build` 返回 `PromptBundle{request: ModelRequest, text: str, metadata: dict}`；`text = request.flatten()`，现有调用方与测试继续可用。
- 历史 message 一旦生成就不再改写。压缩只发生在显式 compaction 点（[spec-06](spec-06-context.md) §4.1）：把 `[i, j)` 区间的 message 替换成一条 `CompactionArtifact` 摘要 message，并在其后重置缓存断点。
- 模式开关 `--context-mode=append_only|rerender`，默认先 `rerender`（今天的行为），评测证明收益后翻默认值。

### 4.3 缓存断点布局

```
[system]                          ← 断点 1（长 TTL）
  identity + rules + tools + skills          （= stable_hash 覆盖范围）
[user #1]
  workspace + repo_map                        （run 内冻结，见 §4.4）
[user/assistant/tool ... 历史 append-only]
  ...
[倒数第 2 条 message]              ← 断点 2（短 TTL）
[最后一条 user]
  memory + relevant + 当前请求                （每轮都变，不缓存）
```

**Anthropic**

```python
payload["system"] = [{"type": "text", "text": head,
                      "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
payload["tools"] = native_tool_definitions(...)        # 也在缓存段内
payload["messages"][-2]["content"][-1]["cache_control"] = {"type": "ephemeral"}
```

**OpenAI `/responses`**：继续 `prompt_cache_key`（= `prefix_state.stable_hash`）+ `prompt_cache_retention`；额外传 `store=False`（不需要服务端留存）。

**能力判定**：新建 `moss/providers/capabilities.py`

```python
@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    supports_native_tools: bool
    supports_cache: bool
    cache_style: str            # none | openai_prefix | anthropic_breakpoint
    reports_cache_usage: bool
    max_output_tokens: int

PROVIDER_CAPABILITIES: dict[tuple[str, str], ModelCapabilities]   # (provider, model_prefix)
def probe(client, first_response_usage) -> ModelCapabilities: ...
```

- 表里查不到 → 保守默认（`supports_cache=False`, `context_window=32000`），并在首个响应后用 usage 字段**探测**：出现 `cache_read_input_tokens`/`cached_tokens` 即翻成 `True` 并记 trace `cache_capability_detected`。
- **不再用 URL substring 判定**。

**修 flag 空转**：`agent_loop` 的判定改成
```python
cache_enabled = agent.feature_enabled("prompt_cache") and caps.supports_cache
```

### 4.4 run 内冻结

- `Moss.begin_run()`：对 `build_skills()` / `build_tools()` 做一次快照，存进 `self._frozen_registry`；`refresh_prefix` 在 run 内只重建 workspace 段，tools/skills 直接用快照。
- 磁盘上 skill 变化 → 落 trace `tool_registry_drift{added, removed}`，**下个 run 生效**。
- REPL 加 `/reload` 命令强制刷新。
- 断言：一次 run 内 `prompt_metadata["prompt_cache_key"]` 恒定。

### 4.5 usage 解析统一

```python
# 统一形状（两个 provider 都产出这个）
{
  "input_tokens": int,
  "output_tokens": int,
  "cache_read_tokens": int,
  "cache_write_tokens": int,
  "cache_metrics_available": bool,
}
```

- Anthropic：读 `usage.input_tokens` / `output_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens`。
- OpenAI：读 `usage.input_tokens` / `output_tokens` / `input_tokens_details.cached_tokens`（`cache_write_tokens` 置 0，OpenAI 不单列）。
- 拿不到任何 cache 字段 → `cache_metrics_available=False`。
- **报告层硬规则**：`cache_metrics_available=False` 时，`cache_hit_rate` 必须打印 `not available`，**禁止渲染成 `0.00%`**。这条在 [spec-08](spec-08-evaluation.md) 的报告渲染里加断言。

### 4.6 协议二选一

| 模式 | prefix | 解析 | 适用 |
| --- | --- | --- | --- |
| `native` | **移除**文本协议段与 `<tool>` 示例（稳定头变短，缓存更省） | 直接消费 `tool_use` block，保留 `call_id`，结果按 `tool_result` 回传 | OpenAI `/responses`、Anthropic `/messages` |
| `text` | 保留现状 | `parse_model_actions(raw, protocol="text")` | Ollama |

- 两套 prefix 各有自己稳定的 `stable_hash`（`prompt_variant` 进 hash 输入）。
- `--tool-protocol=auto|native|text`，默认 `auto`（按能力表）。
- 消除 [moss/providers/clients.py:107](moss/providers/clients.py#L107) 的 XML 中转；`_extract_anthropic_text` 里那段"先扫 tool_use 再回落 text"的补丁随之删除。

### 4.7 prompt 版本化

- `PROMPT_VERSION = "p1"` 常量，进 `report.json` 与 `run_manifest`。
- `.moss/prompts/system.md` 存在则覆盖内置 head，`prompt_version = "file:" + sha256[:12]`。
- 评测把 prompt 版本作为消融维度（[spec-08](spec-08-evaluation.md) §4.8）。

### 4.8 涉及文件

| 文件 | 改动 |
| --- | --- |
| `moss/model_request.py` | 新增（`Block`/`Message`/`ModelRequest`） |
| `moss/providers/capabilities.py` | 新增 |
| [moss/providers/clients.py](moss/providers/clients.py) | 新增 `complete_request(request)`；`complete(prompt, ...)` 变薄封装；Anthropic 接 cache_control + usage；删 XML 中转 |
| [moss/context_manager.py](moss/context_manager.py) | 产出 `PromptBundle`；append-only 模式 |
| [moss/prompt_prefix.py](moss/prompt_prefix.py) | 拆 native/text 两个变体；`prompt_version` |
| [moss/agent_loop.py](moss/agent_loop.py) | 走 `complete_request`；cache flag 判定修正；usage 进 `RunBudget` |
| [moss/runtime.py](moss/runtime.py) | `begin_run` 冻结注册表；`/reload` |
| [moss/cli.py](moss/cli.py) | `--context-mode`、`--tool-protocol`、`--no-prompt-cache` |

## 5. 兼容与迁移

- `complete(prompt: str, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None, tools=None)` 签名**保留不变**，内部转成 `ModelRequest`。`FakeModelClient` 与全部现有测试不受影响。
- `ContextManager.build` 继续返回 `(prompt, metadata)` 二元组；`PromptBundle` 通过 `metadata["_bundle"]` 或新方法 `build_bundle()` 暴露，避免一次性改所有调用点。
- 移除文本协议段会改变 `stable_hash` → 缓存冷启一次，可接受；但**必须与 [spec-02](spec-02-agent-loop.md) 的 `update_plan` 新工具一起上线**，避免连续两次冷启。
- `cache_hit_rate` 口径变化：历史报告里的 0% 需要按 [spec-08](spec-08-evaluation.md) §4.12 第 6 条重新标注为"不可用"，不能直接当成"命中率低"。

## 6. 测试计划

| 测试 | 断言 |
| --- | --- |
| `tests/test_model_request.py`（新） | `flatten()` 与今天的 prompt 文本逐字节一致（对同一份 sections） |
| `tests/test_provider_payload.py`（新，golden） | Anthropic payload：system 带 `cache_control` + `ttl:1h`；tools 在缓存段；倒数第 2 条 message 有第二断点；`prompt_cache=false` 时**不含任何 cache 字段** |
| 同上 | OpenAI payload：`prompt_cache_key` 存在且等于 `stable_hash`；`store=False` |
| 同上 | **仓库内容与工具输出不出现在 system**（注入防线的结构化断言） |
| `tests/test_usage_parsing.py`（新） | Anthropic/OpenAI 两种 usage JSON → 统一形状；缺字段时 `cache_metrics_available=False` |
| `tests/test_cache_stability.py`（新） | 一次 run 内 `prompt_cache_key` 恒定；run 中途新增 `.moss/skills/x.md` 只落 `tool_registry_drift`，key 不变 |
| `tests/test_tool_protocol.py`（新） | native 模式 prefix 不含 `<tool>` 示例；`call_id` 原样回传；多个 tool_use 全部解析 |
| `tests/test_moss.py`（扩展） | `--no-prompt-cache` 时 payload 无 cache 字段（今天会漏） |
| 报告层 | `cache_metrics_available=False` 时渲染出 `not available` 而非 `0.00%` |

## 7. 验收标准

| 指标 | 门槛 |
| --- | --- |
| `cache_read_tokens / input_tokens`（多步任务，默认 provider） | ≥60% |
| 同一任务总输入成本 | 下降 ≥50% |
| 缓存命中率随步数的衰减 | append-only 模式下不衰减（rerender 模式会衰减到 0，作为对照） |
| run 内 `prompt_cache_key` 稳定性 | 100%（断言测试） |
| 解析失败（`retry`）率 | native 模式下相对文本模式下降 |
| 伪 0 指标 | 0（要么真值，要么 `not available`） |

## 8. 实施顺序（PR 拆分）

1. **PR-1（P0，S）**：修 `prompt_cache` flag 空转 + Anthropic usage 解析 + `cache_metrics_available`。**这一个 PR 就能让报告不再撒谎**，优先做。
2. **PR-2（P0，S）**：run 内冻结 tools/skills + `prompt_cache_key` 稳定性测试。
3. **PR-3（P0，M）**：`moss/model_request.py` + `complete_request` + `flatten()` 兼容层（不改行为，纯重构，靠 `flatten` 逐字节对拍）。
4. **PR-4（P0，M）**：角色分层 + `<tool_result>` 包裹（与 [spec-03](spec-03-tool-safety.md) PR-3 合并）。
5. **PR-5（P0，M）**：Anthropic `cache_control` 断点 + capabilities 表 + 探测。
6. **PR-6（P1，M）**：native/text 协议二选一 + `call_id`（与 [spec-02](spec-02-agent-loop.md) PR-4 配合）。
7. **PR-7（P1，M）**：append-only 历史模式（依赖 [spec-06](spec-06-context.md) 的 compaction 落地）。
8. **PR-8（P2，S）**：prompt 版本化 + 文件覆盖。

## 9. 风险与回退

| 风险 | 缓解 |
| --- | --- |
| 结构化重构改变 prompt 文本，行为漂移 | PR-3 用 `flatten()` 对拍逐字节一致，纯重构不改语义 |
| provider payload 格式漂移（模型版本升级） | golden test + capabilities 表按 `model_revision` 记入 manifest；探测失败自动降级为无缓存 |
| 断点位置不对导致只写不读（花钱没收益） | 报告同时打印 `cache_write_tokens`；只写不读连续 3 轮 → stderr 告警 |
| append-only 让 prompt 单调增长 | 必须与 [spec-06](spec-06-context.md) 的 compaction 同期上线；未上线前保持 `rerender` 默认 |
| 移除文本协议后小模型能力不足 | `--tool-protocol=text` 强制回退；Ollama 默认就是 text |
| 缓存冷启一次 | 与其它改稳定头的 PR 合并上线，只冷启一次 |

## 10. 开放问题

1. 1h TTL 的写入成本更高，短任务可能得不偿失。是否按"预计 run 步数"选 5m/1h？倾向：先固定 1h（本地 coding 任务通常多轮），用 `cache_write/read` 比值验证后再调。
2. `workspace` 段放第一条 user message（run 内冻结）还是每轮刷新？冻结能吃到断点 1 的缓存，但 `git status` 会过期。倾向：冻结，变更信息由工具结果与快照 diff 提供。
3. OpenAI 侧要不要也显式打断点（新 API 支持时）？倾向：能力表里留 `cache_style` 字段，支持了再切。
