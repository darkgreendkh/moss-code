#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from moss.atomic_io import write_json_atomic  # noqa: E402
from moss.evaluation.mining import mine_tasks  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mine reproducible coding tasks from local Git history.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--out", required=True)
    parser.add_argument("--since")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    diagnostics = []
    def progress(index, total, commit, found):
        print(f"[{index}/{total}] {commit[:7]} ({found}/{args.limit} tasks)", file=sys.stderr)

    tasks = mine_tasks(
        args.repo,
        since=args.since,
        limit=args.limit,
        diagnostics=diagnostics,
        progress=progress,
    )
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        write_json_atomic(output / f"{task['task_id']}.json", task)
    (output / "exclusions.jsonl").write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in diagnostics
        ),
        encoding="utf-8",
    )
    print(f"mined {len(tasks)} task(s); excluded {len(diagnostics)} commit(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
