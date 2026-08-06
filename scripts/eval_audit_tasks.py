#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from moss.evaluation.audit import audit_task_bank  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mutation-audit Moss task schema v2 files.")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--quarantine", default="benchmarks/quarantine.jsonl")
    args = parser.parse_args(argv)
    summary = audit_task_bank(args.paths, repo_root=args.repo, quarantine_path=args.quarantine)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if summary["quarantined"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
