#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from moss.evaluation.adversarial import load_scenarios  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lint and expand the adversarial scenario matrix.")
    parser.add_argument("path", nargs="?", default="benchmarks/adversarial/scenario-matrix.json")
    args = parser.parse_args(argv)
    scenarios = load_scenarios(args.path)
    print(json.dumps({"scenario_count": len(scenarios), "valid": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
