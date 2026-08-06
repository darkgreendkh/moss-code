# Evaluation Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fully implement `docs/specs/spec-08-evaluation.md` in thirteen independently tested and committed stages, then push `main`.

**Architecture:** Keep deterministic scripted runs as an explicitly labeled L1 contract suite, and add separate L2 capability, L3 adversarial, and L4 cost-utility modules around a versioned task schema. All experiments execute in scratch workspaces; verification restores trusted tests into an isolated copy, statistics and reports reject unsupported claims, and manifests make every trial auditable without adding runtime dependencies.

**Tech Stack:** Python 3.10+ stdlib, pytest, ruff, JSON/JSONL, Markdown, Git and subprocess isolation; no new runtime dependency.

## Global Constraints

- Preserve existing evaluation entry points and historical artifact readers.
- Keep runtime dependencies stdlib-only.
- Never let an evaluation entry point mutate the caller's real repository unless `allow_dirty_workspace=True` is explicit.
- L1 artifacts must never be presented as model capability evidence.
- A judge may add a partial score or review flag but may never determine binary pass alone.
- Unknown pricing stays `None`, never `0`.
- Infrastructure failures are separate from capability failures and remain in the started-trial denominator.
- Work directly on `main`, test and commit every numbered task independently, and push only after the final full regression passes.

---

### Task 1: Evaluation Hygiene

**Files:**
- Create: `tests/test_eval_hygiene.py`
- Modify: `moss/evaluation/evaluator.py`
- Modify: `moss/evaluation/metrics.py`
- Modify: `benchmarks/results/main-resume-repro-2026-06-07/DATA_PROVENANCE.md`
- Modify: archived report artifacts under `benchmarks/results/main-resume-repro-2026-06-07/`
- Modify: `docs/superpowers/plans/2026-08-06-evaluation-framework.md`

**Interfaces:**
- Produces: `_assert_scratch_workspace(root, allow_dirty=False)` and scratch-copy helpers shared by every experiment entry point.
- Produces: one canonical row status field and artifacts without private `_artifact_path` fields.

- [ ] Write failing tests for real-workspace rejection, explicit override, UTC clock use, dynamic facts, private artifact fields, and the single status convention.
- [ ] Run `uv run --with pytest python -m pytest tests/test_eval_hygiene.py tests/test_evaluator.py tests/test_metrics.py -q` and confirm RED for the missing guard and stale artifact behavior.
- [ ] Add the scratch guard to experiment entry points, replace `utcnow()` with `clock.now()`, derive facts from registries/path APIs, remove private fields, normalize status, and correct provenance/archive wording.
- [ ] Run the focused tests plus `uv run ruff check moss/evaluation tests/test_eval_hygiene.py tests/test_evaluator.py tests/test_metrics.py`.
- [ ] Commit as `fix(eval): enforce evaluation hygiene`.

### Task 2: Evaluation Levels and Contract-Suite Migration

**Files:**
- Create: `moss/evaluation/levels/__init__.py`
- Create: `moss/evaluation/levels/l1_contract.py`
- Create: `moss/evaluation/levels/l2_capability.py`
- Create: `moss/evaluation/levels/l3_adversarial.py`
- Create: `moss/evaluation/analysis/__init__.py`
- Create: `moss/evaluation/analysis/report.py`
- Create: `tests/test_report_render.py`
- Modify: `moss/evaluation/evaluator.py`
- Modify: `benchmarks/coding_tasks.json`
- Modify: `tests/test_evaluator.py`

**Interfaces:**
- Produces: `LevelDefinition`, `LEVELS`, `render_layered_report`, and L1 forwarding entry points.
- Adds: `eval_level="L1"`, `suite="contract-smoke"`, and artifact schema version 3 while reading v2 as legacy.

- [ ] Write failing tests for mandatory limitations, v2 legacy headers, L1 artifact labels, and all twelve migrated tasks.
- [ ] Run focused evaluator/report tests and confirm RED.
- [ ] Implement level definitions/rendering and route existing harness entry points through `levels.l1_contract` without changing their public signatures.
- [ ] Run focused tests and lint.
- [ ] Commit as `refactor(eval): separate contract and capability levels`.

### Task 3: Statistical Claims and Report Gates

**Files:**
- Create: `moss/evaluation/stats.py`
- Create: `tests/test_stats.py`
- Modify: `moss/evaluation/analysis/report.py`
- Modify: `tests/test_report_render.py`

**Interfaces:**
- Produces: `wilson_interval`, `pass_hat_k`, `success_at_k`, `cluster_bootstrap`, `paired_bootstrap`, and `rule_of_three`.
- Enforces: comparisons carry literal `n` and a 95% interval; zero incidents carry the rule-of-three upper bound; L2 rows carry cost fields.

- [ ] Write literal/table and seeded-distribution tests for all estimators and report validation failures.
- [ ] Run stats/report tests and confirm RED.
- [ ] Implement dependency-free estimators, deterministic bootstrap sampling, and strict report claim validation.
- [ ] Run focused tests and lint.
- [ ] Commit as `feat(eval): add statistical claim gates`.

### Task 4: Isolated Executable Verification

**Files:**
- Create: `moss/evaluation/verifier.py`
- Create: `tests/test_verifier.py`
- Modify: `moss/evaluation/evaluator.py`

**Interfaces:**
- Produces: frozen `ExecutableSpec`, `VerificationRun`, and `VerificationResult`.
- Produces: `run_verification(task, agent_workspace, fixture_workspace, trace_events=())` with argv execution, timeout, clean env, trusted-file restoration, and hack/corrupt-success detection.

- [ ] Write failing tests for test-file edits, skip/exit/config hacks, hidden-test reads, capability-denied bypass, timeout, clean env, and verification-copy isolation.
- [ ] Run verifier tests and confirm RED.
- [ ] Implement normalized specs, static diff checks, trace checks, clean env reuse, and isolated trusted-file verification.
- [ ] Route v2 verifier dictionaries through the new API while preserving legacy string verifiers in L1.
- [ ] Run focused tests and lint.
- [ ] Commit as `feat(eval): isolate and harden verification`.

### Task 5: Pricing and Trial Cost Accounting

**Files:**
- Create: `moss/evaluation/pricing.py`
- Create: `tests/test_pricing.py`
- Modify: `moss/evaluation/levels/l2_capability.py`
- Modify: `moss/evaluation/analysis/report.py`
- Modify: `moss/evaluation/metrics.py`

**Interfaces:**
- Produces: frozen `Price`, dated `PRICE_TABLE`, `estimate_cost`, `TrialMetrics`, and equal-budget completion summaries.
- Records: pass, USD, wall time, token classes, model turns, and tool calls for every L2 trial.

- [ ] Write failing tests for known/unknown prices, cache-token accounting, budget completion, and Pareto rendering.
- [ ] Run pricing/report tests and confirm RED.
- [ ] Implement dated pricing, cost aggregation, `None` propagation, equal-budget filtering, and text Pareto output.
- [ ] Run focused tests and lint.
- [ ] Commit as `feat(eval): account for cost and latency`.

### Task 6: Task Schema and Git-History Mining

**Files:**
- Create: `benchmarks/schema/task-v2.schema.json`
- Create: `moss/evaluation/task_schema.py`
- Create: `moss/evaluation/mining.py`
- Create: `scripts/eval_lint_tasks.py`
- Create: `scripts/eval_mine.py`
- Create: `tests/test_task_schema.py`
- Create: `tests/test_mining.py`
- Create: generated draft tasks under `benchmarks/tasks/mined/`

**Interfaces:**
- Produces: `validate_task`, `lint_task_paths`, `mine_tasks(repo_root, since=None, limit=50)`, and CLI exit codes.
- Mining requires source+test commits, parent fail, commit pass three times, stable archive SHA, and never changes caller cwd/worktree.

- [ ] Write failing schema/lint tests and temporary-repository mining tests for valid, invalid, and flaky commits.
- [ ] Run mining/schema tests and confirm RED.
- [ ] Implement the stdlib schema validator, archive/overlay builder, three-run validator, difficulty buckets, provenance, and CLIs.
- [ ] Mine and store at least twenty reproducible draft tasks from the current Git history, recording exclusions without activating unreviewed prompts.
- [ ] Run focused tests, lint the generated task bank, and lint Python.
- [ ] Commit as `feat(eval): mine reproducible coding tasks`.

### Task 7: Held-Out Tests, Mutation Audit, and Quarantine

**Files:**
- Modify: `moss/evaluation/verifier.py`
- Modify: `moss/evaluation/task_schema.py`
- Create: `moss/evaluation/audit.py`
- Create: `scripts/eval_audit_tasks.py`
- Modify: `tests/test_verifier.py`
- Create: `tests/test_task_audit.py`

**Interfaces:**
- Adds visible/hidden results, `overfit_to_visible`, three negative-patch categories, and append-only quarantine records.
- Produces: `audit_task` and `audit_task_bank`; any surviving negative patch quarantines the task and invalidates affected conclusions.

- [ ] Write failing held-out, mutation rejection, quarantine, and historical-invalidation tests.
- [ ] Run verifier/audit tests and confirm RED.
- [ ] Implement separate visible/hidden execution, negative-patch application, quarantine persistence, and audit CLI.
- [ ] Run focused tests and lint.
- [ ] Commit as `feat(eval): audit held-out and mutation resistance`.

### Task 8: Honest Context, Memory, and Recovery Ablations

**Files:**
- Create: `moss/evaluation/ablations.py`
- Create: `tests/test_memory_ablation_guard.py`
- Create: `tests/test_ablations.py`
- Modify: `moss/evaluation/metrics.py`
- Modify: `tests/test_metrics.py`

**Interfaces:**
- Produces context variants with success/token/latency and at least two compactions, cross-run memory variants with prompt-leak rejection and false-memory metrics, and recovery kill-boundary metrics with zero duplicate side effects.
- Keeps old v2 entry points as deprecated L1-compatible adapters.

- [ ] Write failing tests for prompt fact leakage, required variant sets, compaction count, cross-run identity, recovery boundaries, and metric completeness.
- [ ] Run ablation tests and confirm RED.
- [ ] Implement experiment plans/validators and adapt existing experiment entry points to honest layer labels and triplet metrics.
- [ ] Run focused tests and lint.
- [ ] Commit as `refactor(eval): make ablations capability-aware`.

### Task 9: Failure Taxonomy and Trajectory Analysis

**Files:**
- Create: `moss/evaluation/failure_taxonomy.py`
- Create: `moss/evaluation/analysis/trajectory.py`
- Create: `tests/test_failure_taxonomy.py`
- Modify: `moss/evaluation/analysis/report.py`

**Interfaces:**
- Produces the complete Spec-08 label set, `label_trial(trace_events, task, diff)`, coverage measurement, histograms, and run-id drill-down.

- [ ] Write positive and negative examples for every label plus mixed-trace coverage tests.
- [ ] Run taxonomy tests and confirm RED.
- [ ] Implement rule-first labeling, trajectory features, coverage aggregation, and report rendering.
- [ ] Run focused tests and lint.
- [ ] Commit as `feat(eval): classify trajectory failures`.

### Task 10: Parallel Runs, Manifests, Infra Failures, and CI Tiers

**Files:**
- Create: `moss/evaluation/manifest.py`
- Create: `moss/evaluation/runner.py`
- Create: `tests/test_eval_manifest.py`
- Create: `tests/test_eval_runner.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `moss/evaluation/analysis/report.py`

**Interfaces:**
- Produces frozen `RunManifest`, incomplete-to-complete artifact publication, `ProcessPoolExecutor` execution with isolated `.moss`, and two-denominator summaries.
- CI runs L0/L1 on changes and exposes scheduled/manual L2/L3 jobs.

- [ ] Write failing manifest completeness, per-worker isolation, infra-failure denominator, publication, and CI-contract tests.
- [ ] Run runner/manifest tests and confirm RED.
- [ ] Implement manifest capture, process workers, retry-preserving result records, atomic completion, and tiered workflow jobs.
- [ ] Run focused tests and lint.
- [ ] Commit as `feat(eval): parallelize auditable trial runs`.

### Task 11: Adversarial Scenario Bank

**Files:**
- Create: `benchmarks/tasks/adversarial/scenarios.json`
- Modify: `moss/evaluation/levels/l3_adversarial.py`
- Create: `tests/test_adversarial_eval.py`
- Modify: `moss/evaluation/analysis/report.py`

**Interfaces:**
- Produces at least thirty scenarios across README, comments, test output, package metadata, and AGENTS.md injection surfaces and all five attack targets.
- Reports ASR, refusal, utility retention, false positives, approval burden, and defense-variant safety/utility points.

- [ ] Write failing scenario-coverage and metric tests with deterministic safe/unsafe trial fixtures.
- [ ] Run adversarial tests and confirm RED.
- [ ] Add the scenario bank, validator, aggregate metrics, and safety-utility rendering.
- [ ] Run focused tests and lint.
- [ ] Commit as `feat(eval): add adversarial safety suite`.

### Task 12: Calibrated Optional Judge

**Files:**
- Create: `moss/evaluation/judge.py`
- Create: `benchmarks/gold/README.md`
- Create: `benchmarks/gold/calibration.jsonl`
- Create: `tests/test_judge.py`
- Modify: `moss/evaluation/analysis/report.py`

**Interfaces:**
- Produces frozen `JudgeVerdict`, blind request construction, Cohen's kappa/correlation calibration, and a 15% judge-cost sampling guard.
- Enforces deterministic binary pass ownership and marks κ below 0.7 as uncalibrated.

- [ ] Write failing verdict, blinding, binary-pass, calibration, threshold-label, and cost-guard tests.
- [ ] Run judge tests and confirm RED.
- [ ] Implement judge parsing, prompt hashing, gold loading, calibration statistics, review routing, and report labels; include fifty clearly marked annotation slots without invented human judgments.
- [ ] Run focused tests and lint.
- [ ] Commit as `feat(eval): add calibrated optional judge`.

### Task 13: Optional Public Benchmark Adapter and Documentation

**Files:**
- Create: `moss/evaluation/adapters/__init__.py`
- Create: `moss/evaluation/adapters/swe_task.py`
- Create: `tests/test_swe_adapter.py`
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `docs/specs/spec-08-evaluation.md`

**Interfaces:**
- Produces a dependency-free adapter from SWE-style task dictionaries to task schema v2 without downloading data or claiming public scores.
- Documents L0-L4 meanings, commands, artifacts, external-cost gates, and the boundary between implemented infrastructure and unrun live experiments.

- [ ] Write failing adapter and documentation-contract tests.
- [ ] Run focused tests and confirm RED.
- [ ] Implement the adapter, mark Spec 08 implemented with explicit live-evidence boundaries, and add operator commands.
- [ ] Run focused tests, lint, and `git diff --check`.
- [ ] Commit as `docs(eval): finish spec-08 evaluation framework`.

### Final Verification and Push

- [ ] Run `uv run --with pytest python -m pytest tests/ -q` and confirm zero failures.
- [ ] Run `uv run ruff check moss tests scripts` and confirm zero findings.
- [ ] Run `git diff --check` and all task/scenario lint and audit commands.
- [ ] Re-read `docs/specs/spec-08-evaluation.md` sections 2–10 and map every requirement to implementation/tests; report real-model/manual-label evidence as pending rather than fabricating results.
- [ ] Confirm a clean worktree and inspect the thirteen stage commits with `git log --oneline origin/main..HEAD`.
- [ ] Push with `git push origin main`; never force-push.
