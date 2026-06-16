import subprocess
import sys
from pathlib import Path

import mini_moss


def test_mini_moss_module_and_public_exports():
    assert mini_moss.Moss is not None
    assert mini_moss.FakeModelClient is not None
    assert not hasattr(mini_moss, "MiniAgent")
    result = subprocess.run([sys.executable, "-m", "mini_moss", "--help"], capture_output=True, text=True, check=True)
    assert "Teaching-sized Moss agent harness" in result.stdout


def test_readme_main_mapping_points_to_existing_files():
    repo_root = Path(__file__).resolve().parents[3]
    main_files = [
        "moss/cli.py",
        "moss/runtime.py",
        "moss/agent_loop.py",
        "moss/context_manager.py",
        "moss/providers/clients.py",
        "moss/tool_executor.py",
        "moss/tools.py",
        "moss/task_state.py",
        "moss/run_store.py",
        "moss/workspace.py",
    ]
    for path in main_files:
        assert (repo_root / path).exists()
