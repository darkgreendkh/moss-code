#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from moss.evaluation.task_schema import lint_task_paths  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate Moss evaluation task schema v2 files.")
    parser.add_argument("paths", nargs="+", help="Task JSON files or directories.")
    args = parser.parse_args(argv)
    errors = lint_task_paths(args.paths)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
