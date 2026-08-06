# Moss Capability Package Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `moss/` 从 46 个根级模块重组为按能力分包的结构，并把 `Moss` 收缩成保持公共 API 兼容的组合式 facade。

**Architecture:** 先用纯移动和 import 更新让目录表达现有能力边界，再分别拆 `cli`、`tools`、`ContextManager` 和 `Moss`。移动与行为修改不混在同一提交；`moss/__init__.py` 和 CLI 始终保持兼容。

**Tech Stack:** Python 3.10+ 标准库、pytest、ruff、setuptools；不新增第三方运行时依赖。

## Global Constraints

- `pyproject.toml` 的运行时 `dependencies` 继续为空。
- 全量测试必须零失败；不能把已知失败当作绿色基线。
- 所有工具调用继续经过 `Moss.run_tool()` / `Moss.execute()` 与 `ToolExecutor`。
- trace 事件只从统一事件模块引用；事件名、schema 和落盘格式不变。
- CLI 的 stdout/stderr 契约、配置优先级、`.moss/` 落盘结构不变。
- 公共调用 `from moss import Moss, SessionStore, WorkspaceContext` 保持可用。
- 保留当前工作区已有修改；未明确归属的旧改动不得被重构提交顺带纳入。

---

## Locked File Map

第一轮只移动完整模块，不拆函数：

| 当前路径 | 目标路径 |
| --- | --- |
| `moss/agent_loop.py` | `moss/agent/loop.py` |
| `moss/output_parser.py` | `moss/agent/output_parser.py` |
| `moss/task_state.py` | `moss/agent/state.py` |
| `moss/budget.py` | `moss/agent/budget.py` |
| `moss/stall.py` | `moss/agent/stall.py` |
| `moss/verification.py` | `moss/agent/verification.py` |
| `moss/context_manager.py` | `moss/context/manager.py` |
| `moss/model_request.py` | `moss/context/model_request.py` |
| `moss/prompt_prefix.py` | `moss/context/prefix.py` |
| `moss/token_budget.py` | `moss/context/token_budget.py` |
| `moss/compaction.py` | `moss/context/compaction.py` |
| `moss/output_compressors.py` | `moss/context/compressors.py` |
| `moss/workspace.py` | `moss/context/repository/workspace.py` |
| `moss/repo_map.py` | `moss/context/repository/repo_map.py` |
| `moss/ignore.py` | `moss/context/repository/ignore.py` |
| `moss/retrieval.py` | `moss/context/repository/retrieval.py` |
| `moss/tool_executor.py` | `moss/execution/executor.py` |
| `moss/tools.py` | `moss/execution/registry.py` |
| `moss/tool_context.py` | `moss/execution/protocol.py` |
| `moss/policy.py` | `moss/execution/safety/policy.py` |
| `moss/shell_policy.py` | `moss/execution/safety/shell.py` |
| `moss/sandbox.py` | `moss/execution/safety/sandbox.py` |
| `moss/injection.py` | `moss/execution/safety/injection.py` |
| `moss/security.py` | `moss/execution/safety/secrets.py` |
| `moss/features/memory.py` | `moss/memory/service.py` |
| `moss/features/memory_records.py` | `moss/memory/records.py` |
| `moss/features/memory_store.py` | `moss/memory/store.py` |
| `moss/run_store.py` | `moss/runs/store.py` |
| `moss/run_index.py` | `moss/runs/index.py` |
| `moss/session_store.py` | `moss/runs/session.py` |
| `moss/checkpoint.py` | `moss/runs/checkpoint.py` |
| `moss/lease.py` | `moss/runs/lease.py` |
| `moss/action_ledger.py` | `moss/runs/ledger.py` |
| `moss/rewind.py` | `moss/runs/rewind.py` |
| `moss/trace_events.py` | `moss/runs/observability/events.py` |
| `moss/trace_html.py` | `moss/runs/observability/html.py` |
| `moss/otel.py` | `moss/runs/observability/otel.py` |
| `moss/skills.py` | `moss/extensions/skills.py` |
| `moss/hooks.py` | `moss/extensions/hooks.py` |
| `moss/delegation.py` | `moss/extensions/delegation.py` |
| `moss/model_router.py` | `moss/extensions/router.py` |
| `moss/code_mode.py` | `moss/extensions/code_mode.py` |
| `moss/mcp/` | `moss/extensions/mcp/` |

`moss/atomic_io.py`、`moss/clock.py`、`moss/config.py`、`moss/runtime.py`、
`moss/__init__.py`、`moss/__main__.py` 在第一轮保持原路径。

---

### Task 0: Recover a Green Baseline

**Files:**
- Modify: `tests/test_artifact_offload.py`
- Modify: `tests/test_swe_adapter.py`

**Interfaces:**
- Consumes: 当前工作区把 artifact 阈值设为 `MAX_TOOL_OUTPUT=16000`，并把评测文档移到 `docs/features/evaluation.md`。
- Produces: 一个与当前未提交实现一致、全量零失败的测试基线。

- [x] **Step 1: Run the full suite and record the existing failures**

Run: `uv run --with pytest python -m pytest tests/ -q`

Observed: `1185 passed, 2 failed`；失败为 artifact 测试夹具低于新阈值，以及评测文档旧路径。

- [x] **Step 2: Make the artifact fixture unambiguously exceed 16000 characters**

Use 900 lines and request `end=900`，使测试仍然验证“卸载前脱敏”，而不是钉死旧阈值。

- [x] **Step 3: Point the documentation test at the moved feature guide**

Read `docs/features/evaluation.md` and assert its current evidence-boundary wording `不能声称模型能力`。

- [x] **Step 4: Run the two previously failing tests**

Run:
`uv run --with pytest python -m pytest tests/test_artifact_offload.py::test_artifact_content_is_redacted_before_it_lands_on_disk tests/test_swe_adapter.py::test_documentation_marks_live_evidence_and_manual_gold_as_pending -q`

Expected: `2 passed`.

- [x] **Step 5: Re-run the full suite**

Run: `uv run --with pytest python -m pytest tests/ -q`

Observed: `1187 passed, 16 warnings`；warnings 均为现有评测兼容入口和 tar 提取的弃用告警。

---

### Task 1: Lock the Target Package Shape and Import Direction

**Files:**
- Create: `tests/test_package_layout.py`
- Create: `moss/agent/__init__.py`
- Create: `moss/context/__init__.py`
- Create: `moss/context/repository/__init__.py`
- Create: `moss/execution/__init__.py`
- Create: `moss/execution/safety/__init__.py`
- Create: `moss/memory/__init__.py`
- Create: `moss/runs/__init__.py`
- Create: `moss/runs/observability/__init__.py`
- Create: `moss/extensions/__init__.py`

**Interfaces:**
- Consumes: ADR 0004 的目标目录和依赖规则。
- Produces: `TARGET_PACKAGES`、`ROOT_MODULE_ALLOWLIST`、`MIGRATION_TARGETS` 和禁止能力包反向导入 facade 的 AST 契约。

- [x] **Step 1: Write the failing layout test**

```python
from pathlib import Path

TARGET_PACKAGES = {
    "agent", "context", "execution", "memory", "runs", "extensions",
    "providers", "evaluation",
}
ROOT_MODULE_ALLOWLIST = {
    "__init__.py", "__main__.py", "atomic_io.py", "clock.py",
    "config.py", "runtime.py",
}
# MIGRATION_TARGETS 逐项包含 Locked File Map 中所有根级 .py 迁移，
# 另加 "cli.py": "cli/__init__.py"；完整字典落在 tests/test_package_layout.py。


def test_runtime_modules_live_in_capability_packages():
    root = Path("moss")
    packages = {path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith("__")}
    assert TARGET_PACKAGES <= packages
    root_modules = {path.name for path in root.glob("*.py")}
    assert root_modules <= ROOT_MODULE_ALLOWLIST | MIGRATION_TARGETS.keys()
    for source, target in MIGRATION_TARGETS.items():
        assert (root / source).exists() != (root / target).exists()
```

- [x] **Step 2: Run it and verify it fails on the current flat modules**

Run: `uv run --with pytest python -m pytest tests/test_package_layout.py -q`

Expected: FAIL because the capability package directories do not exist yet. The migration map keeps the test green while each file moves, and its XOR assertion prevents duplicate old/new implementations.

- [x] **Step 3: Add an AST rule that capability packages do not import facade modules**

The same test file parses every `moss/{agent,context,execution,memory,runs,extensions}/**/*.py`.
Fail when an `Import` or `ImportFrom` resolves to `moss.runtime` or `moss.cli`.
The test excludes `moss/evaluation/` because evaluation intentionally consumes the public runtime.

- [x] **Step 4: Create package directories and empty `__init__.py` files**

Do not export implementation classes yet; exports are added with each move so incomplete packages cannot look usable. Re-run `tests/test_package_layout.py`; it must now pass before commit.

- [x] **Step 5: Commit the contract test and skeleton**

```bash
git add tests/test_package_layout.py moss/agent moss/context moss/execution moss/memory moss/runs moss/extensions
git commit -m "test(architecture): lock capability package boundaries"
```

---

### Task 2: Move Memory and Run Persistence Capabilities

**Files:**
- Move: the memory and runs rows from `Locked File Map`
- Modify: `moss/runtime.py`, `moss/cli.py`, `moss/agent_loop.py`, `moss/tool_executor.py`
- Modify: `moss/providers/recording.py`, `moss/evaluation/**/*.py`
- Modify: tests importing `moss.features.*`, `moss.run_store`, `moss.run_index`, `moss.session_store`, `moss.checkpoint`, `moss.lease`, `moss.action_ledger`, `moss.rewind`, `moss.trace_events`, `moss.trace_html`, or `moss.otel`

**Interfaces:**
- Produces: `moss.memory.{service,records,store}` and `moss.runs.*`; `moss.memory.__init__` exports `MemoryStore`, `MemoryRecord`, `SourceRef`, `project_scope_key`.
- Preserves: JSONL schemas, run directory paths, trace event string values, `SessionStore` public re-export from `moss`.

- [x] **Step 1: Move memory modules without editing behavior**
- [x] **Step 2: Update relative imports inside memory and all consumers**
- [x] **Step 3: Run memory-focused tests**

Run: `uv run --with pytest python -m pytest tests/ -q -k "memory or retrieval or checkpoint or rewind"`

- [x] **Step 4: Move run, session, checkpoint, ledger, rewind and observability modules**
- [x] **Step 5: Update all consumers and trace-event AST checks to the new paths**
- [x] **Step 6: Run persistence and artifact tests**

Run: `uv run --with pytest python -m pytest tests/test_run_store.py tests/test_run_index.py tests/test_session_store_v2.py tests/test_checkpoint.py tests/test_action_receipt.py tests/test_rewind.py tests/test_artifact_offload.py tests/test_trace_events.py -q`

- [x] **Step 7: Run full tests and lint, then commit**

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
git diff --check
git add moss tests
git commit -m "refactor(packages): group memory and run persistence"
```

---

### Task 3: Move Context and Repository Capabilities

**Files:**
- Move: all context and context/repository rows from `Locked File Map`
- Modify: `moss/runtime.py`, `moss/cli.py`, `moss/agent_loop.py`, provider recording, evaluation and tests

**Interfaces:**
- Produces: `moss.context.manager.ContextManager`, `moss.context.model_request.ModelRequest`, `moss.context.prefix.PromptPrefix`, and `moss.context.repository.workspace.WorkspaceContext`.
- Preserves: prompt block order, native tool-call grouping, cache keys, token budgets, workspace fingerprints and repo-map cache schema.

- [x] **Step 1: Move repository-context leaf modules and update imports**
- [x] **Step 2: Run workspace/repo-map/retrieval tests**

Run: `uv run --with pytest python -m pytest tests/ -q -k "workspace or repo_map or ignore or retrieval"`

- [x] **Step 3: Move prompt/context modules and update imports**
- [x] **Step 4: Run prompt, cache, native protocol and compaction tests**

Run: `uv run --with pytest python -m pytest tests/test_context_manager.py tests/test_prompt_prefix.py tests/test_cache_stability.py tests/test_tool_protocol.py tests/test_compaction.py -q`

- [x] **Step 5: Update `moss/__init__.py` so `WorkspaceContext` still imports from `moss`**
- [x] **Step 6: Run full tests and lint, then commit**

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
git diff --check
git add moss tests
git commit -m "refactor(packages): group prompt and repository context"
```

---

### Task 4: Move Execution Safety and Extension Capabilities

**Files:**
- Move: all execution, execution/safety and extensions rows from `Locked File Map`
- Modify: `moss/runtime.py`, `moss/cli.py`, `moss/agent_loop.py`, providers, evaluation and tests

**Interfaces:**
- Produces: `moss.execution.executor.ToolExecutor`, `moss.execution.registry.ToolSpec`, `moss.execution.protocol.ActionRequest`, `moss.extensions.mcp.*`.
- Preserves: ToolExecutor guard order, approval behavior, shell risk levels, sandbox degradation reporting, capability fail-closed policy and MCP JSON-RPC behavior.

- [ ] **Step 1: Move safety leaf modules and update consumers**
- [ ] **Step 2: Run safety and policy tests**

Run: `uv run --with pytest python -m pytest tests/ -q -k "policy or shell or sandbox or injection or security or approval"`

- [ ] **Step 3: Move tool protocol, registry and executor modules**
- [ ] **Step 4: Move extensions and MCP modules**
- [ ] **Step 5: Run tool, skill, hook, delegate, code-mode and MCP tests**

Run: `uv run --with pytest python -m pytest tests/ -q -k "tool or skill or hook or delegate or code_mode or mcp"`

- [ ] **Step 6: Run full tests and lint, then commit**

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
git diff --check
git add moss tests
git commit -m "refactor(packages): group execution safety and extensions"
```

---

### Task 5: Move Agent Control Modules and Make the Layout Test Green

**Files:**
- Move: all agent rows from `Locked File Map`
- Modify: `moss/runtime.py`, `moss/cli.py`, `moss/evaluation/**/*.py`, tests and `tests/test_package_layout.py`

**Interfaces:**
- Produces: `moss.agent.loop.AgentLoop`, `moss.agent.state.TaskState`, `moss.agent.output_parser.Action`.
- Preserves: stop reasons, multi-action writeback order, one-shot exit status and budget behavior.

- [ ] **Step 1: Move agent modules and update all imports**
- [ ] **Step 2: Run loop, parser, budget, stall and verification tests**

Run: `uv run --with pytest python -m pytest tests/ -q -k "agent_loop or output_parser or budget or stall or verification or task_state"`

- [ ] **Step 3: Run the package-layout test**

Run: `uv run --with pytest python -m pytest tests/test_package_layout.py -q`

Expected: PASS; all agent modules are at their target paths. `cli.py` remains the only planned root migration and is removed in Task 6.

- [ ] **Step 4: Run full tests and lint, then commit**

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
git diff --check
git add moss tests
git commit -m "refactor(packages): complete capability module layout"
```

---

### Task 6: Split CLI, Tool Registry and Context Rendering

**Files:**
- Replace: `moss/cli.py` with `moss/cli/__init__.py`, `parser.py`, `factory.py`, `repl.py`, `commands/runs.py`, `commands/memory.py`, `commands/mcp.py`
- Split: `moss/execution/registry.py` into `specs.py`, `builtins/files.py`, `builtins/shell.py`, `builtins/memory.py`, `builtins/extensions.py`, keeping `registry.py` as the assembler
- Create: `moss/context/history.py`, `moss/context/native_history.py`, `moss/context/health.py`
- Modify: `pyproject.toml`, `moss/__init__.py`, affected tests

**Interfaces:**
- Produces: `moss.cli.main`, `build_agent`, `build_arg_parser`, `build_welcome`; `moss.execution.registry.build_tools`; pure context rendering helpers.
- Preserves: console script target `moss.cli:main`, tool names/schemas/order, prompt bytes for existing fixtures.

- [ ] **Step 1: Add characterization assertions for public CLI exports and deterministic tool schema order**
- [ ] **Step 2: Watch the new tests fail when importing the not-yet-created submodules**
- [ ] **Step 3: Split CLI by command responsibility and keep re-exports in `moss/cli/__init__.py`**
- [ ] **Step 3a: Remove `cli.py` from `MIGRATION_TARGETS` and assert the final root allowlist directly**
- [ ] **Step 4: Split tool specs from built-in implementations; keep explicit registry assembly**
- [ ] **Step 5: Extract native-history grouping and context-health calculation as pure helpers**
- [ ] **Step 6: Run CLI, tools and context tests**

Run: `uv run --with pytest python -m pytest tests/test_cli.py tests/test_tools.py tests/test_context_manager.py tests/test_tool_protocol.py -q`

- [ ] **Step 7: Run full tests and lint, then commit**

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
git diff --check
git add moss tests pyproject.toml
git commit -m "refactor(core): split cli tools and context rendering"
```

---

### Task 7: Shrink `Moss` into a Composition Facade

**Files:**
- Create: `moss/context/service.py`
- Create: `moss/execution/service.py`
- Create: `moss/runs/coordinator.py`
- Create: `moss/extensions/manager.py`
- Modify: `moss/runtime.py`
- Modify: focused runtime tests

**Interfaces:**
- `ContextService(agent)` owns prefix refresh, context building, repo-map anchors, token calibration and compaction delegation.
- `ExecutionService(agent)` owns tool registry, approval, execution, artifact storage and injection state.
- `RunCoordinator(agent)` owns run lifecycle, trace/report/checkpoint/rewind and task IDs.
- `ExtensionManager(agent)` owns skill activation, hooks, delegation, model routing, MCP and code mode.
- `Moss` constructs these four objects and preserves existing public method signatures by one-line delegation.

- [ ] **Step 1: Add characterization tests for each public facade cluster**

Tests call `Moss` public methods, then assert the existing observable result: context metadata, tool receipt, run report, skill activation or delegate contract. They must not assert the private component object itself.

- [ ] **Step 2: Extract `RunCoordinator` and make its characterization tests pass**
- [ ] **Step 3: Extract `ContextService` and make its characterization tests pass**
- [ ] **Step 4: Extract `ExecutionService` and make its characterization tests pass**
- [ ] **Step 5: Extract `ExtensionManager` and make its characterization tests pass**
- [ ] **Step 6: Assert `runtime.py` no longer owns implementation methods from the four clusters**

Use an AST test to cap `Moss` at the explicitly documented public facade methods; do not use a raw line-count assertion.

- [ ] **Step 7: Run full tests and lint, then commit**

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
git diff --check
git add moss tests
git commit -m "refactor(runtime): make Moss a composition facade"
```

---

### Task 8: Synchronize Architecture Documentation and Close the Plan

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/architecture.md`
- Modify: `docs/README.md`
- Modify: affected `docs/features/*.md` and `docs/reference/*.md` code paths
- Move: this plan from `docs/plans/active/` to `docs/plans/archive/`
- Modify: `docs/plans/archive/README.md`

**Interfaces:**
- Produces: one current package map, updated code anchors and an archived completed plan.

- [ ] **Step 1: Update the architecture tree and module map to the final paths**
- [ ] **Step 2: Update every old `moss/<flat_module>.py` reference**

Run an explicit `rg` scan for every source name in `Locked File Map`; remaining hits are allowed only inside this archived migration plan and Git history discussion.

- [ ] **Step 3: Update `CLAUDE.md` invariants only where paths changed**
- [ ] **Step 4: Move the completed plan to archive and add it to the archive index**
- [ ] **Step 5: Run final verification**

```bash
uv run --with pytest python -m pytest tests/ -q
uv run ruff check moss tests scripts
python -m compileall -q moss
git diff --check
git status --short
```

Expected: zero test failures, zero ruff errors, compileall success, no whitespace errors, and only intended files changed.

- [ ] **Step 6: Commit documentation**

```bash
git add CLAUDE.md docs
git commit -m "docs(architecture): document capability package layout"
```
