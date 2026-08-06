# Prompt Composition and Cache Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fully implement `docs/specs/spec-04-prompt-cache.md` in eight independently tested and committed stages, then push `main`.

**Architecture:** Introduce a provider-neutral structured `ModelRequest`, keep the current string API as a compatibility adapter, and move provider payload construction/usage parsing behind provider clients. Freeze cache-sensitive registries per run, add explicit capability and protocol selection, preserve a rerender compatibility mode while offering append-only history, and version the stable prompt head.

**Tech Stack:** Python 3.10+, stdlib dataclasses/JSON/urllib/hashlib, pytest, ruff; no new runtime dependency.

## Global Constraints

- Preserve `complete(prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None, tools=None)`.
- Repository content and tool output must never be placed in `ModelRequest.system`.
- `prompt_cache` must require both the feature flag and provider capabilities.
- Unknown providers/models fail closed with cache disabled and a 32,000-token context window.
- Default context mode remains `rerender`; default tool protocol is `auto`.
- Anthropic long cache breakpoint uses `{"type": "ephemeral", "ttl": "1h"}`.
- OpenAI Responses requests use `store=False`.
- Runtime dependencies remain stdlib-only.
- Each numbered task ends with focused tests and one commit on `main`.

---

### Task 1: Honest Cache Flag and Usage Metrics

**Files:**
- Create: `tests/test_usage_parsing.py`
- Modify: `moss/providers/clients.py`
- Modify: `moss/agent_loop.py`
- Modify: `moss/evaluation/metrics.py`
- Modify: `tests/test_moss.py`
- Modify: `tests/test_metrics.py`
- Create: `docs/superpowers/plans/2026-08-05-prompt-cache.md`

**Interfaces:**
- Produces: `parse_openai_usage(data) -> dict` and `parse_anthropic_usage(data) -> dict` with `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, and `cache_metrics_available`.
- Produces: cache report values that are numeric only when telemetry is available; otherwise render `not available`.

- [ ] Write failing provider and report tests using literal OpenAI/Anthropic usage fixtures and a disabled `prompt_cache` feature flag.
- [ ] Run `.venv/bin/python -m pytest tests/test_usage_parsing.py tests/test_moss.py tests/test_metrics.py -q` and confirm failures are caused by missing unified fields/flag enforcement.
- [ ] Implement minimal usage normalizers; retain `cached_tokens`/`cache_hit` compatibility aliases; gate cache arguments in `AgentLoop` with `agent.feature_enabled("prompt_cache")`.
- [ ] Update metric aggregation so unavailable runs are excluded from cache-rate denominators and Markdown uses the literal `not available`.
- [ ] Re-run focused tests and `uv run ruff check moss tests scripts`.
- [ ] Commit as `feat(cache): report cache telemetry honestly`.

### Task 2: Freeze Cache-Sensitive Registries per Run

**Files:**
- Create: `tests/test_cache_stability.py`
- Modify: `moss/runtime.py`
- Modify: `moss/agent_loop.py`
- Modify: `moss/cli.py`
- Modify: `moss/trace_events.py`
- Modify: `tests/test_skills.py`

**Interfaces:**
- Produces: `Moss.begin_run()`, `Moss.end_run()`, and `Moss.reload_registry()`.
- Produces: trace event `tool_registry_drift` with literal `added` and `removed` name lists.

- [ ] Write failing tests proving a skill added between two model turns does not change the run cache key, emits drift, and becomes visible after `/reload` or the next run.
- [ ] Run `python -m pytest tests/test_cache_stability.py tests/test_skills.py -q` and confirm key drift/reload failures.
- [ ] Snapshot `skills` and `tools` at `begin_run`; refresh only workspace state during a run; compare live discovery for drift without applying it.
- [ ] Call `begin_run`/`end_run` from the loop's existing run lifecycle and add the REPL `/reload` command.
- [ ] Re-run focused tests and lint.
- [ ] Commit as `feat(cache): freeze prompt registries within a run`.

### Task 3: Provider-Neutral Structured Model Requests

**Files:**
- Create: `moss/model_request.py`
- Create: `tests/test_model_request.py`
- Modify: `moss/context_manager.py`
- Modify: `moss/providers/clients.py`
- Modify: `moss/agent_loop.py`

**Interfaces:**
- Produces immutable `Block`, `Message`, `ModelRequest`, and `PromptBundle` dataclasses.
- Produces `ContextManager.build_bundle(user_message) -> PromptBundle` while `build()` keeps returning `(text, metadata)`.
- Produces `complete_request(request)` on every model client; legacy `complete()` adapts a flat user request.

- [ ] Write failing tests with hand-built sections proving `ModelRequest.flatten()` equals the legacy assembled prompt byte-for-byte.
- [ ] Run `python -m pytest tests/test_model_request.py tests/test_context_manager.py tests/test_moss.py -q` and verify the structured API is missing.
- [ ] Add immutable request dataclasses and a deterministic flatten format matching `_assemble_prompt`.
- [ ] Add `build_bundle`; store the bundle in internal metadata for the loop without serializing it into artifacts; route the loop through `complete_request` when supported.
- [ ] Re-run focused tests and lint.
- [ ] Commit as `refactor(prompt): add structured model requests`.

### Task 4: Enforce Prompt Role and Trust Boundaries

**Files:**
- Create: `tests/test_provider_payload.py`
- Modify: `moss/model_request.py`
- Modify: `moss/context_manager.py`
- Modify: `moss/providers/clients.py`
- Modify: `tests/test_injection.py`

**Interfaces:**
- `ModelRequest.system` contains only platform-trusted identity/rules/tools/skills blocks.
- Workspace, memory, relevant context, history, request, and tool results remain separate non-system blocks/messages.
- Tool output flattening retains `<tool_result untrusted="true" source="...">`.

- [ ] Write failing golden payload tests asserting workspace marker and injected tool text are absent from Anthropic `system` and OpenAI `developer` content.
- [ ] Run `python -m pytest tests/test_provider_payload.py tests/test_injection.py -q` and confirm flat-role payload failures.
- [ ] Split the stable prompt head from workspace text, map request blocks to provider roles, and preserve current flattened text for compatibility clients.
- [ ] Re-run focused tests and lint.
- [ ] Commit as `feat(prompt): separate trusted rules from repository data`.

### Task 5: Provider Capabilities and Anthropic Cache Breakpoints

**Files:**
- Create: `moss/providers/capabilities.py`
- Modify: `moss/providers/__init__.py`
- Modify: `moss/providers/clients.py`
- Modify: `moss/agent_loop.py`
- Modify: `moss/trace_events.py`
- Modify: `tests/test_provider_payload.py`
- Modify: `tests/test_usage_parsing.py`

**Interfaces:**
- Produces immutable `ModelCapabilities` and `capabilities_for(provider, model)`.
- Produces response-driven `probe(capabilities, usage) -> ModelCapabilities`.
- Anthropic payload adds the long system breakpoint and short penultimate-message breakpoint only when cache is enabled.

- [ ] Write failing table/default/probe tests and exact Anthropic/OpenAI payload assertions, including no cache fields when disabled.
- [ ] Run the capability and provider payload tests and confirm URL-substring/cache-control failures.
- [ ] Replace URL substring detection with capability lookup; implement conservative probing and `cache_capability_detected` trace emission.
- [ ] Build Anthropic cache breakpoints and OpenAI `prompt_cache_key`/retention plus `store=False` from the structured request.
- [ ] Re-run focused tests and lint.
- [ ] Commit as `feat(cache): add provider capabilities and breakpoints`.

### Task 6: Native and Text Tool Protocols

**Files:**
- Create: `tests/test_tool_protocol.py`
- Modify: `moss/model_request.py`
- Modify: `moss/prompt_prefix.py`
- Modify: `moss/providers/clients.py`
- Modify: `moss/output_parser.py`
- Modify: `moss/agent_loop.py`
- Modify: `moss/runtime.py`
- Modify: `moss/cli.py`

**Interfaces:**
- CLI/runtime protocol values: `auto`, `native`, `text`; resolved request protocols: `native`, `text`.
- Native provider results return all ordered tool calls with their original `call_id`, without XML conversion.
- Native tool results are serialized back using each provider's required `tool_result`/`function_call_output` shape.

- [ ] Write failing tests for native prefix omission, multiple tool calls, ordered `call_id` preservation, and forced text fallback.
- [ ] Run protocol/parser/client tests and confirm XML conversion/first-call failures.
- [ ] Give prefix building a protocol variant included in `stable_hash`; add native action extraction; parse native actions directly and preserve all IDs.
- [ ] Add `--tool-protocol` and choose `auto` from capabilities, with Ollama resolving to text.
- [ ] Re-run focused tests and lint.
- [ ] Commit as `feat(tools): support native and text protocols`.

### Task 7: Append-Only Context Mode

**Files:**
- Create: `tests/test_append_only_context.py`
- Modify: `moss/model_request.py`
- Modify: `moss/context_manager.py`
- Modify: `moss/runtime.py`
- Modify: `moss/cli.py`

**Interfaces:**
- CLI/runtime context modes: `rerender`, `append_only`; default `rerender`.
- Append-only mode converts persisted session entries to stable `Message` objects without rewriting prior content.
- A compaction artifact, when present, replaces exactly its declared `[start, end)` message range and carries a cache reset marker.

- [ ] Write failing tests proving old message serialization stays byte-identical when a new turn is appended and rerender remains the default.
- [ ] Run `python -m pytest tests/test_append_only_context.py tests/test_context_manager.py tests/test_moss.py -q` and confirm missing mode behavior.
- [ ] Implement append-only message construction and explicit compaction-artifact replacement while retaining the old renderer for `rerender`.
- [ ] Add `--context-mode` plumbing and metadata identifying the selected mode.
- [ ] Re-run focused tests and lint.
- [ ] Commit as `feat(context): add append-only prompt history`.

### Task 8: Prompt Versioning and File Override

**Files:**
- Modify: `moss/prompt_prefix.py`
- Modify: `moss/runtime.py`
- Modify: `moss/checkpoint.py`
- Modify: `tests/test_prompt_prefix.py`
- Modify: `tests/test_run_artifacts.py`
- Modify: `CLAUDE.md`
- Modify: `.env.example`

**Interfaces:**
- Produces `PROMPT_VERSION = "p1"`.
- `.moss/prompts/system.md` overrides the built-in stable head and produces `file:<sha256[:12]>`.
- `prompt_version` is persisted in prompt metadata, `report.json`, and runtime identity/run manifest data.

- [ ] Write failing tests for built-in/file versions, stable-hash changes, and persisted report/runtime identity fields.
- [ ] Run focused prompt/artifact tests and confirm missing version fields.
- [ ] Implement file override loading, version calculation, persistence, and concise operator documentation.
- [ ] Run focused tests, then `python -m pytest tests/ -q`, then `uv run ruff check moss tests scripts`.
- [ ] Confirm `git status --short`, review all eight commits, and commit as `feat(prompt): version the system prompt`.
- [ ] Push with `git push origin main`; retry only transient TLS/network failures.
