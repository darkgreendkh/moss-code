#!/usr/bin/env python3
"""录制 L1 磁带（spec-09 §9.8）。

两种来源，manifest 里如实记 `source`：

- `--source scripted`（默认）：拿现有 `SCRIPTED_MODEL_OUTPUTS` 当模型，把真实的
  **请求指纹**录下来。这不是真实模型轨迹，只是把 L1 的驱动方式从"按顺序吐脚本"
  换成"按请求指纹匹配"——harness 改动导致的请求变化会立刻表现为 miss。
- `--source provider`：接真实后端录一次。这才是 spec 说的真实轨迹，
  但需要 API key 和花钱，所以不在 CI 里跑。

**录制必须走 `BenchmarkEvaluator` 自己的任务执行路径**，不能在这里复制一份。
复制一份的后果是隐蔽的：少调一次 `_apply_task_setup`，prompt 就差一段，
录出来的指纹和回放时算出来的对不上，全部 miss。

用法：

    python3 scripts/record_cassettes.py --task readme_intro_locked
    python3 scripts/record_cassettes.py --all
"""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from moss.evaluation import cassettes  # noqa: E402
from moss.evaluation.evaluator import (  # noqa: E402
    DEFAULT_BENCHMARK_PATH,
    SCRIPTED_MODEL_OUTPUTS,
    BenchmarkEvaluator,
    _scripted_outputs_for_task,
)
from moss.context.prefix import PROMPT_VERSION  # noqa: E402
from moss.providers.clients import FakeModelClient  # noqa: E402
from moss.providers.recording import Cassette, RecordingModelClient  # noqa: E402


def _inner_client(source, task):
    if source == "scripted":
        return FakeModelClient(_scripted_outputs_for_task(task))
    # 真实后端：沿用 CLI 的装配路径，避免这里再复制一份 provider 选择逻辑。
    from moss.cli import _build_model_client, build_arg_parser

    return _build_model_client(build_arg_parser().parse_args([]))


def record(benchmark_path, task_ids, source, repo_root=ROOT):
    """跑一遍 benchmark，把选中任务的模型调用录进各自的磁带目录。"""
    recorded = {}

    def factory(task, workspace):
        inner = _inner_client(source, task)
        if task["id"] not in task_ids:
            return inner
        directory = cassettes.cassette_dir(repo_root, task["id"])
        # 重录就是重录。留着旧文件会让序号错位，回放时按文件名排序读到半新半旧。
        shutil.rmtree(directory, ignore_errors=True)
        client = RecordingModelClient(
            inner,
            directory,
            root=workspace.repo_root,
            prompt_version=PROMPT_VERSION,
        )
        recorded[task["id"]] = client
        return client

    scratch = Path(tempfile.mkdtemp(prefix="moss-cassette-"))
    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=scratch / "recording-artifact.json",
        workspace_root=scratch / "workspaces",
        model_client_factory=factory,
    )
    artifact = evaluator.run()

    manifests = []
    for task_id, client in sorted(recorded.items()):
        cassette = Cassette(client.cassette.directory)
        manifest = cassette.read_manifest()
        manifest["source"] = (
            cassettes.SOURCE_SCRIPTED_BOOTSTRAP if source == "scripted" else cassettes.SOURCE_PROVIDER
        )
        manifest["task_id"] = task_id
        manifest["entry_count"] = len(cassette.entry_paths())
        # 磁带自带出处，否则半年后没人知道这盘带子是怎么来的、能不能拿来下结论。
        cassette.write_manifest(manifest)
        manifests.append(manifest)
    return manifests, artifact


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Record L1 replay cassettes for benchmark tasks.")
    parser.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK_PATH))
    parser.add_argument("--task", action="append", default=[], help="Task id; repeatable.")
    parser.add_argument("--all", action="store_true", help="Record every task that has scripted outputs.")
    parser.add_argument(
        "--source",
        choices=("scripted", "provider"),
        default="scripted",
        help="Where the model outputs come from. provider costs money and needs an API key.",
    )
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    benchmark_path = ROOT / args.benchmark
    wanted = set(args.task)
    if args.all:
        wanted = set(SCRIPTED_MODEL_OUTPUTS)
    skipped = wanted & set(cassettes.UNCASSETTABLE_TASKS)
    for task_id in sorted(skipped):
        print(f"skipping {task_id}: {cassettes.UNCASSETTABLE_TASKS[task_id]}", file=sys.stderr)
    wanted -= skipped
    if not wanted:
        print("nothing to record: pass --task or --all", file=sys.stderr)
        return 1
    manifests, artifact = record(benchmark_path, wanted, args.source)
    if not manifests:
        print("no matching tasks in the benchmark", file=sys.stderr)
        return 1
    # 录制过程本身也是一次 L1 跑批。它没通过就说明录进去的是一条失败轨迹。
    failures = [row["id"] for row in artifact["rows"] if row["status"] != "pass"]
    if failures:
        print(f"warning: recorded a failing trajectory for: {', '.join(failures)}", file=sys.stderr)
    print(json.dumps(manifests, indent=2, sort_keys=True, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
