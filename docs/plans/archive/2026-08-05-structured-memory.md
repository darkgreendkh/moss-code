# Structured Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Spec 05's structured memory system in eight independently tested and committed stages, then push `main`.

**Architecture:** Keep transient working and episodic state in `LayeredMemory`, move durable truth to an atomically persisted append-only record store, and use one dependency-free BM25 implementation for automatic and explicit recall. Runtime integrations remain behind the existing `ToolExecutor`; memory write tools call narrow `ToolContext` callbacks, while migration, conflict handling, scope, eviction, and distillation stay inside focused memory modules.

**Tech Stack:** Python 3.10+, stdlib only, pytest, ruff, JSONL, Markdown projections.

## Global Constraints

- Preserve the zero-third-party runtime dependency rule.
- Do not create `moss/memory.py`; new memory modules live under `moss/features/`.
- Preserve existing `LayeredMemory` public call signatures.
- Persist every durable-memory rewrite atomically with a same-directory temporary file and `os.replace`.
- Treat memory as untrusted reference data, never as instructions.
- Work directly on `main`, test and commit each stage independently, and push only after the full regression is green.

---

### Task 1: BM25 Retrieval and Recall Evaluation

**Files:**
- Create: `moss/retrieval.py`
- Create: `tests/test_retrieval.py`
- Create: `tests/test_memory_recall.py`
- Modify: `moss/features/memory.py`
- Modify: `moss/context_manager.py`
- Modify: `docs/superpowers/plans/2026-08-05-structured-memory.md`

**Interfaces:**
- Produces: `Hit(doc_id: str, score: float, breakdown: dict)`.
- Produces: `BM25Index.add(doc_id, fields, weight=1.0, ts=None)` and `BM25Index.search(query, limit=5, min_score=0.0, now=None)`.
- Preserves: `LayeredMemory.retrieval_candidates(query, limit=3) -> list[dict]`.

- [ ] Add behavior tests proving IDF ranking, length normalization, CJK bigram recall, abstention, score breakdown, and an nDCG@3 improvement of at least 30% over the old overlap baseline.
- [ ] Run `uv run --with pytest python -m pytest tests/test_retrieval.py tests/test_memory_recall.py -q` and confirm the missing module/API causes the expected RED failure.
- [ ] Implement the dependency-free index and switch episodic, file-summary, and legacy durable recall to it; expose retrieval explanations through prompt metadata.
- [ ] Run `uv run --with pytest python -m pytest tests/test_retrieval.py tests/test_memory_recall.py tests/test_memory.py tests/test_context_manager.py -q` and confirm GREEN.
- [ ] Run `uv run ruff check moss/retrieval.py moss/features/memory.py moss/context_manager.py tests/test_retrieval.py tests/test_memory_recall.py`.
- [ ] Commit with `git commit -m "feat(memory): rank recall with BM25"`.

### Task 2: Durable Record Store and Migration

**Files:**
- Create: `moss/features/memory_records.py`
- Create: `moss/features/memory_store.py`
- Create: `tests/test_memory_records.py`
- Create: `tests/test_memory_migration.py`
- Modify: `moss/features/memory.py`

**Interfaces:**
- Produces: frozen `SourceRef` and `MemoryRecord` dataclasses with schema version 1 and JSON conversion.
- Produces: `MemoryStore.append`, `write`, `update`, `delete`, `active_records`, `all_records`, `rebuild_projections`, and `migrate_legacy`.
- Preserves: `LayeredMemory.promote_durable(promotions) -> (promoted, superseded)`.

- [ ] Add tests for append-only event semantics, supersede visibility, tombstone permanence, atomic compaction, trailing partial-line recovery, projection equivalence, backup creation, and idempotent Markdown migration.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_records.py tests/test_memory_migration.py -q` and confirm the missing record/store APIs cause RED.
- [ ] Implement schema validation, deterministic IDs, atomic JSONL replacement, latest-event folding, projection generation, compaction above 2000 lines, and one-time legacy migration with `MEMORY.md.bak`.
- [ ] Replace the legacy durable implementation inside `LayeredMemory` while retaining `MOSS_MEMORY_V2=off` as a one-release fallback.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_records.py tests/test_memory_migration.py tests/test_memory.py tests/test_moss.py -q` and confirm GREEN.
- [ ] Run `uv run ruff check moss/features/memory_records.py moss/features/memory_store.py moss/features/memory.py tests/test_memory_records.py tests/test_memory_migration.py`.
- [ ] Commit with `git commit -m "feat(memory): add durable record store"`.

### Task 3: Trust and Poisoning Defenses

**Files:**
- Create: `tests/test_memory_security.py`
- Modify: `moss/features/memory.py`
- Modify: `moss/features/memory_store.py`
- Modify: `moss/trace_events.py`
- Modify: `moss/runtime.py`
- Modify: `moss/prompt_prefix.py`

**Interfaces:**
- Produces: `LayeredMemory.write_durable(..., trust, source_refs) -> (MemoryRecord | None, reason)`.
- Produces: trace event `memory_poisoning_blocked`.
- Enforces: non-legacy durable records always have source references; tool-derived content remains episodic.

- [ ] Add tests proving injected repository text never becomes durable, injection and secret-shaped writes return structured rejection reasons, poisoning emits a trace event, and rendered memory carries trust/source labels plus a data-only warning.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_security.py -q` and confirm RED on the missing validation path.
- [ ] Apply redaction, noise validation, injection scanning, trust admission, source-reference enforcement, and prompt labeling before durable persistence.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_security.py tests/test_injection.py tests/test_security.py tests/test_memory.py -q` and confirm GREEN.
- [ ] Run `uv run ruff check moss/features/memory.py moss/features/memory_store.py moss/trace_events.py moss/runtime.py moss/prompt_prefix.py tests/test_memory_security.py`.
- [ ] Commit with `git commit -m "feat(memory): enforce trust and poisoning checks"`.

### Task 4: Agent and CLI Memory Tools

**Files:**
- Create: `tests/test_memory_tools.py`
- Modify: `moss/tools.py`
- Modify: `moss/tool_context.py`
- Modify: `moss/runtime.py`
- Modify: `moss/cli.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_prompt_prefix.py`

**Interfaces:**
- Produces tools: `memory_write(scope, topic, text, tags)`, `memory_update(id, text)`, `memory_delete(id)`, and `memory_search(query, limit=5)`.
- Produces CLI: `moss memory list|show|add|forget|export`.
- Restricts model writes to `session|project`; global writes are available only through CLI.

- [ ] Add end-to-end tool tests for schemas, capabilities, runtime execution, duplicate/refusal output, update/delete/search behavior, no-hit abstention, and stable prompt signature changes; add CLI tests for all five subcommands including global add.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_tools.py tests/test_tools.py tests/test_prompt_prefix.py -q` and confirm RED on missing tools.
- [ ] Add narrow memory callbacks to `ToolContext`, register all four non-risky tools, implement runtime callbacks and CLI dispatch, and render `/memory` with trust and source.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_tools.py tests/test_tools.py tests/test_prompt_prefix.py tests/test_policy.py tests/test_moss.py -q` and confirm GREEN.
- [ ] Run `uv run ruff check moss/tools.py moss/tool_context.py moss/runtime.py moss/cli.py tests/test_memory_tools.py tests/test_tools.py tests/test_prompt_prefix.py`.
- [ ] Commit with `git commit -m "feat(memory): expose memory tools and CLI"`.

### Task 5: Conflict Resolution and Freshness

**Files:**
- Create: `tests/test_memory_conflict.py`
- Modify: `moss/features/memory_records.py`
- Modify: `moss/features/memory_store.py`
- Modify: `moss/features/memory.py`

**Interfaces:**
- Produces: alias-backed `normalize_subject(text, aliases)`.
- Produces: trust/observation-time conflict resolution and source SHA freshness checks.
- Preserves tombstone and superseded records for explicit inspection while hiding them from normal recall.

- [ ] Add tests for higher-trust supersession, same-trust newer supersession, near-simultaneous contradiction review, aliases, stale source marking, and absence of conflicting active recall results.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_conflict.py -q` and confirm RED.
- [ ] Implement `aliases.md` parsing, stable subject normalization, conflict state transitions, recall penalties/labels, and on-recall source SHA verification.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_conflict.py tests/test_memory_records.py tests/test_memory_tools.py -q` and confirm GREEN.
- [ ] Run `uv run ruff check moss/features/memory_records.py moss/features/memory_store.py moss/features/memory.py tests/test_memory_conflict.py`.
- [ ] Commit with `git commit -m "feat(memory): resolve conflicting facts"`.

### Task 6: Value Eviction and Cold Storage

**Files:**
- Create: `tests/test_memory_eviction.py`
- Modify: `moss/features/memory.py`
- Modify: `moss/features/memory_store.py`
- Modify: `moss/context_manager.py`

**Interfaces:**
- Produces: value score `hit_count + 2*used_count + 1.5*recency_score + trust_weight`.
- Produces: cold episodic JSONL at `.moss/memory/episodic/<session>.jsonl`.
- Ensures: automatic prompt recall uses the hot set; explicit search includes hot and cold notes.

- [ ] Add a 50-note long-session test proving low-value notes are cold-stored, an early valuable note remains explicitly searchable, tombstones never revive, and rendered memory remains within its token budget.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_eviction.py -q` and confirm RED.
- [ ] Replace FIFO slicing with budget-aware value eviction, persist evictees atomically, index cold notes only for explicit search, and update hit/use counters.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_eviction.py tests/test_memory.py tests/test_context_manager.py -q` and confirm GREEN.
- [ ] Run `uv run ruff check moss/features/memory.py moss/features/memory_store.py moss/context_manager.py tests/test_memory_eviction.py`.
- [ ] Commit with `git commit -m "feat(memory): cold-store low-value notes"`.

### Task 7: Scope Isolation and Symbol Summaries

**Files:**
- Create: `tests/test_memory_scope.py`
- Modify: `moss/features/memory.py`
- Modify: `moss/features/memory_store.py`
- Modify: `moss/runtime.py`
- Modify: `moss/repo_map.py`

**Interfaces:**
- Produces project scope key `sha256(repo_root.resolve())` and current-path scope boosts of 1.5/0.5.
- Produces file summary shape `{path, sha, symbols, summary}` with exact line-range read suggestions.
- Includes global memory at `~/.moss/memory` as a separate store without cross-project leakage.

- [ ] Add tests proving two repositories cannot see one another's project records, path scopes receive the specified boosts, global records are visible where allowed, and file summaries preserve repo-map symbol line ranges.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_scope.py -q` and confirm RED.
- [ ] Implement physical/scoped store routing, scope-key validation and weighting, repo-map symbol attachment, and exact `read_file` suggestions.
- [ ] Run `uv run --with pytest python -m pytest tests/test_memory_scope.py tests/test_repo_map.py tests/test_memory.py tests/test_moss.py -q` and confirm GREEN.
- [ ] Run `uv run ruff check moss/features/memory.py moss/features/memory_store.py moss/runtime.py moss/repo_map.py tests/test_memory_scope.py`.
- [ ] Commit with `git commit -m "feat(memory): isolate scopes and index symbols"`.

### Task 8: Procedural Distillation and Final Integration

**Files:**
- Create: `tests/test_distill.py`
- Modify: `moss/features/memory.py`
- Modify: `moss/features/memory_store.py`
- Modify: `moss/agent_loop.py`
- Modify: `moss/runtime.py`
- Modify: `moss/cli.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Produces: `distill_run(trace_events, mode="rule") -> list[MemoryRecord]`.
- Produces procedural projections at `.moss/memory/procedural/<id>.md` and includes them in recall.
- Produces CLI option `--reflect=off|rule|model`; `model` remains dependency-gated by Spec 09 and uses rule output until an auxiliary model is configured.

- [ ] Add tests for failure-to-success extraction, denied-operation constraints, empty abstention, source event references, procedural persistence/recall, and automatic run-finish distillation.
- [ ] Run `uv run --with pytest python -m pytest tests/test_distill.py -q` and confirm RED.
- [ ] Implement rule distillation, procedural projection/recall, run-finish invocation, reflect configuration, and user-facing documentation.
- [ ] Run `uv run --with pytest python -m pytest tests/test_distill.py tests/test_agent_loop.py tests/test_moss.py -q` and confirm GREEN.
- [ ] Run `uv run ruff check moss tests scripts`.
- [ ] Commit with `git commit -m "feat(memory): distill procedural lessons"`.

### Final Verification and Push

- [ ] Run `uv run --with pytest python -m pytest tests/ -q` and confirm zero failures on this macOS checkout.
- [ ] Run `uv run ruff check moss tests scripts` and confirm zero findings.
- [ ] Re-read `docs/specs/spec-05-memory.md` sections 4–8 and map every requirement to code/tests; record any dependency-gated limitation explicitly.
- [ ] Confirm `git status --short` is empty and inspect the eight stage commits with `git log --oneline origin/main..HEAD`.
- [ ] Push with `git push origin main`; if the documented transient TLS failure occurs, retry without force.
