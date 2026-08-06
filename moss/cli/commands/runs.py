"""Runs command implementation."""

import argparse
import json
from pathlib import Path
import sys

from ...context.token_budget import middle
from ...runs.index import referenced_run_ids
from ...runs.observability.html import render_run_html
from ...runs.observability.otel import trace_to_otlp
from ...runs.store import RunStore

def build_runs_arg_parser():
    parser = argparse.ArgumentParser(prog="moss runs", description="Inspect and manage run artifacts.")
    commands = parser.add_subparsers(dest="runs_command", required=True)

    def add_location(command):
        command.add_argument("--cwd", default=".", help="Workspace whose .moss/runs to use.")

    list_parser = commands.add_parser("list", help="List recent runs from the index.")
    list_parser.add_argument("--limit", type=int, default=20)
    add_location(list_parser)

    show_parser = commands.add_parser("show", help="Show one run's task state and report.")
    show_parser.add_argument("run_id")
    show_parser.add_argument(
        "--html",
        action="store_true",
        help="Emit a single self-contained HTML page (inline CSS/SVG, no external requests).",
    )
    add_location(show_parser)

    verify_parser = commands.add_parser("verify", help="Verify a run's trace hash chain.")
    verify_parser.add_argument("run_id", nargs="?", default="", help="Omit to verify every indexed run.")
    add_location(verify_parser)

    prune_parser = commands.add_parser("prune", help="Archive expired runs to <run_id>.jsonl.gz.")
    prune_parser.add_argument("--keep-count", type=int, default=None)
    prune_parser.add_argument("--keep-days", type=int, default=None)
    prune_parser.add_argument("--dry-run", action="store_true")
    add_location(prune_parser)

    pin_parser = commands.add_parser("pin", help="Pin a run so retention never touches it.")
    pin_parser.add_argument("run_id")
    pin_parser.add_argument("--off", action="store_true", help="Unpin instead.")
    add_location(pin_parser)

    export_parser = commands.add_parser("export", help="Export a run's trace as JSON on stdout.")
    export_parser.add_argument("run_id")
    export_parser.add_argument(
        "--otel",
        action="store_true",
        help="Emit OTLP/JSON spans instead of raw trace events.",
    )
    add_location(export_parser)
    return parser


def _runs_store(args):
    workspace = Path(args.cwd).resolve()
    return RunStore(workspace / ".moss" / "runs"), workspace


def _runs_list_line(entry):
    cost = entry.get("cost_usd")
    cost_text = "-" if cost is None else f"${float(cost):.4f}"
    pin = "*" if entry.get("pinned") else " "
    return (
        f"{pin}{entry.get('run_id', '-'):<32} {str(entry.get('status', '-')):<10} "
        f"{str(entry.get('stop_reason', '') or '-'):<22} {cost_text:>10}  {middle(str(entry.get('task_summary', '')), 48)}"
    )


def run_runs_command(argv):
    args = build_runs_arg_parser().parse_args(argv)
    store, workspace = _runs_store(args)
    command = args.runs_command

    if command == "list":
        store.ensure_index()
        entries = store.index.entries()[: max(1, int(args.limit))]
        print("\n".join(_runs_list_line(entry) for entry in entries) or "(no runs)")
        return 0

    if command == "show":
        if args.html:
            events = store.read_trace(args.run_id)
            if not events:
                print(f"no trace for run: {args.run_id}", file=sys.stderr)
                return 1
            report = store.load_report(args.run_id) if store.report_path(args.run_id).exists() else None
            try:
                task_state = store.load_task_state(args.run_id)
            except (OSError, json.JSONDecodeError):
                # 归档过的 run 只剩 trace。信息不全也得出得来页面——
                # 排查工具在最需要它的时候往往就是信息不全的。
                task_state = None
            print(render_run_html(args.run_id, events, report=report, task_state=task_state), end="")
            return 0
        try:
            payload = {
                "task_state": store.load_task_state(args.run_id),
                "report": store.load_report(args.run_id) if store.report_path(args.run_id).exists() else None,
                "trace_events": len(store.read_trace(args.run_id)),
                "lease": store.lease.read(args.run_id),
            }
        except (OSError, json.JSONDecodeError):
            print(f"run not found: {args.run_id}", file=sys.stderr)
            return 1
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    if command == "verify":
        store.ensure_index()
        run_ids = [args.run_id] if args.run_id else [entry["run_id"] for entry in store.index.entries()]
        failed = 0
        for run_id in run_ids:
            ok, problems = store.verify_trace(run_id)
            print(f"{run_id}: {'ok' if ok else 'BROKEN'}")
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            failed += 0 if ok else 1
        # 审计工件被改过必须让脚本看得见，否则 verify 这个命令没有意义。
        return 1 if failed else 0

    if command == "prune":
        protected = referenced_run_ids(sorted((workspace / "artifacts").glob("*.json")))
        result = store.prune(
            keep_count=args.keep_count,
            keep_days=args.keep_days,
            protected=protected,
            dry_run=bool(args.dry_run),
        )
        verb = "would archive" if args.dry_run else "archived"
        print(json.dumps({verb: result}, ensure_ascii=False, sort_keys=True))
        return 0

    if command == "pin":
        store.pin(args.run_id, pinned=not args.off)
        print(json.dumps({"run_id": args.run_id, "pinned": not args.off}, ensure_ascii=False, sort_keys=True))
        return 0

    events = store.read_trace(args.run_id)
    if not events:
        print(f"no trace for run: {args.run_id}", file=sys.stderr)
        return 1
    payload = trace_to_otlp(args.run_id, events) if args.otel else events
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return 0

