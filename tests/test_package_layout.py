"""源码包边界契约。

迁移期间每个模块必须且只能存在于旧路径或目标路径之一；迁移完成后收紧为
根目录只保留入口、facade 和三个基础模块。这个测试防的是新能力继续随手
落到根目录，以及能力包反向依赖装配层。
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path


TARGET_PACKAGES = {
    "agent",
    "context",
    "evaluation",
    "execution",
    "extensions",
    "memory",
    "providers",
    "runs",
}
ROOT_MODULE_ALLOWLIST = {
    "__init__.py",
    "__main__.py",
    "atomic_io.py",
    "clock.py",
    "config.py",
    "runtime.py",
}
MIGRATION_TARGETS = {
    "action_ledger.py": "runs/ledger.py",
    "agent_loop.py": "agent/loop.py",
    "budget.py": "agent/budget.py",
    "checkpoint.py": "runs/checkpoint.py",
    "cli.py": "cli/__init__.py",
    "code_mode.py": "extensions/code_mode.py",
    "compaction.py": "context/compaction.py",
    "context_manager.py": "context/manager.py",
    "delegation.py": "extensions/delegation.py",
    "hooks.py": "extensions/hooks.py",
    "ignore.py": "context/repository/ignore.py",
    "injection.py": "execution/safety/injection.py",
    "lease.py": "runs/lease.py",
    "model_request.py": "context/model_request.py",
    "model_router.py": "extensions/router.py",
    "otel.py": "runs/observability/otel.py",
    "output_compressors.py": "context/compressors.py",
    "output_parser.py": "agent/output_parser.py",
    "policy.py": "execution/safety/policy.py",
    "prompt_prefix.py": "context/prefix.py",
    "repo_map.py": "context/repository/repo_map.py",
    "retrieval.py": "context/repository/retrieval.py",
    "rewind.py": "runs/rewind.py",
    "run_index.py": "runs/index.py",
    "run_store.py": "runs/store.py",
    "sandbox.py": "execution/safety/sandbox.py",
    "security.py": "execution/safety/secrets.py",
    "session_store.py": "runs/session.py",
    "shell_policy.py": "execution/safety/shell.py",
    "skills.py": "extensions/skills.py",
    "stall.py": "agent/stall.py",
    "task_state.py": "agent/state.py",
    "token_budget.py": "context/token_budget.py",
    "tool_context.py": "execution/protocol.py",
    "tool_executor.py": "execution/executor.py",
    "tools.py": "execution/registry.py",
    "trace_events.py": "runs/observability/events.py",
    "trace_html.py": "runs/observability/html.py",
    "verification.py": "agent/verification.py",
    "workspace.py": "context/repository/workspace.py",
}
CAPABILITY_PACKAGE_ROOTS = {
    "agent",
    "context",
    "execution",
    "extensions",
    "memory",
    "runs",
}
FORBIDDEN_FACADE_IMPORTS = {"moss.cli", "moss.runtime"}


def test_runtime_modules_follow_the_declared_migration_map():
    root = Path("moss")
    packages = {
        path.name
        for path in root.iterdir()
        if path.is_dir() and not path.name.startswith("__")
    }
    assert TARGET_PACKAGES <= packages

    root_modules = {path.name for path in root.glob("*.py")}
    assert root_modules <= ROOT_MODULE_ALLOWLIST | MIGRATION_TARGETS.keys()

    for source, target in MIGRATION_TARGETS.items():
        assert (root / source).exists() != (root / target).exists(), (
            f"exactly one migration location must exist: {source} -> {target}"
        )


def _module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_targets(path: Path) -> set[str]:
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    targets = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = resolve_name("." * node.level + (node.module or ""), package)
                if node.module:
                    targets.add(base)
                else:
                    targets.update(f"{base}.{alias.name}" for alias in node.names)
            elif node.module:
                targets.add(node.module)
    return targets


def test_capability_packages_do_not_import_facades():
    for package in sorted(CAPABILITY_PACKAGE_ROOTS):
        for path in Path("moss", package).rglob("*.py"):
            for target in _import_targets(path):
                assert not any(
                    target == forbidden or target.startswith(f"{forbidden}.")
                    for forbidden in FORBIDDEN_FACADE_IMPORTS
                ), f"{path} imports facade module {target}"
