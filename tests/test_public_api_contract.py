from pathlib import Path

import moss
from moss import Moss, SessionStore, WorkspaceContext, build_agent, build_arg_parser, build_welcome, main


def test_public_api_exports_current_names_only():
    assert Moss is not None
    assert SessionStore is not None
    assert WorkspaceContext is not None
    assert callable(build_agent)
    assert callable(build_arg_parser)
    assert callable(build_welcome)
    assert callable(main)
    assert not hasattr(moss, "MiniAgent")
    assert "MiniAgent" not in moss.__all__


def test_build_agent_returns_moss(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])

    agent = build_agent(args)

    assert isinstance(agent, Moss)


def test_lightweight_package_split_uses_package_paths_without_legacy_shims():
    from moss.evaluation.evaluator import BenchmarkEvaluator
    from moss.evaluation.metrics import run_context_ablation_v2
    from moss.features.memory import LayeredMemory
    from moss.providers.clients import FakeModelClient as ProviderFakeModelClient

    assert BenchmarkEvaluator is not None
    assert LayeredMemory is not None
    assert ProviderFakeModelClient is not None
    assert callable(run_context_ablation_v2)
    for legacy_module in ("evaluator.py", "metrics.py", "models.py", "memory.py"):
        assert not (Path("moss") / legacy_module).exists()


def test_packaging_discovers_moss_subpackages():
    pyproject_text = Path("pyproject.toml").read_text(encoding="utf-8")

    assert "[tool.setuptools.packages.find]" in pyproject_text
    assert 'include = ["moss*"]' in pyproject_text
