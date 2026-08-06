"""Memory command implementation."""

import argparse
import json
from pathlib import Path
import sys

from ...execution.safety import injection as injectionlib
from ...memory.records import SourceRef, make_record
from ...memory.service import reject_memory_reason
from ...memory.store import MemoryStore, project_scope_key

def build_memory_arg_parser():
    parser = argparse.ArgumentParser(prog="moss memory", description="Inspect and manage durable memory.")
    commands = parser.add_subparsers(dest="memory_command", required=True)

    def add_location(command, *, include_scope=True):
        command.add_argument("--cwd", default=".", help="Workspace for project memory.")
        if include_scope:
            command.add_argument("--scope", choices=("project", "global"), default="project")

    list_parser = commands.add_parser("list", help="List active memories.")
    add_location(list_parser)

    show_parser = commands.add_parser("show", help="Show one memory record.")
    show_parser.add_argument("id")
    add_location(show_parser)

    add_parser = commands.add_parser("add", help="Add an explicit user memory.")
    add_parser.add_argument("text")
    add_parser.add_argument("--topic", required=True)
    add_parser.add_argument("--tag", action="append", default=[])
    add_location(add_parser)

    forget_parser = commands.add_parser("forget", help="Append a tombstone for one memory.")
    forget_parser.add_argument("id")
    add_location(forget_parser)

    export_parser = commands.add_parser("export", help="Export active memories as JSON.")
    add_location(export_parser)
    return parser


def _memory_cli_store(args):
    if args.scope == "global":
        return MemoryStore(Path.home() / ".moss" / "memory"), "global"
    workspace = Path(args.cwd).resolve()
    return MemoryStore(workspace / ".moss" / "memory", workspace_root=workspace), project_scope_key(workspace)


def _memory_cli_source(record):
    if not record.source_refs:
        return "unknown"
    return ",".join(source.path or source.run_id or "unknown" for source in record.source_refs)


def _memory_cli_line(record):
    return (
        f"{record.id} [{record.scope} trust={record.trust} source={_memory_cli_source(record)}] "
        f"{record.topic}: {record.text}"
    )


def run_memory_command(argv):
    args = build_memory_arg_parser().parse_args(argv)
    store, scope_key = _memory_cli_store(args)
    command = args.memory_command

    if command == "list":
        records = store.active_records()
        print("\n".join(_memory_cli_line(record) for record in records) or "(no memories)")
        return 0
    if command == "show":
        record = store.get(args.id)
        if record is None:
            print(f"memory not found: {args.id}", file=sys.stderr)
            return 1
        print(_memory_cli_line(record))
        return 0
    if command == "export":
        print(json.dumps([record.to_dict() for record in store.active_records()], ensure_ascii=False, sort_keys=True))
        return 0
    if command == "forget":
        try:
            record = store.delete(args.id)
        except KeyError:
            print(f"memory not found: {args.id}", file=sys.stderr)
            return 1
        print(json.dumps({"deleted": record.id}, ensure_ascii=False, sort_keys=True))
        return 0

    text = str(args.text).strip()
    finding = injectionlib.scan(text, source="memory_cli")
    reason = "injection_suspected" if finding is not None else reject_memory_reason(text)
    if reason:
        reason = "too_noisy" if reason == "noisy_output" else reason
        print(json.dumps({"status": "rejected", "reason": reason}, ensure_ascii=False, sort_keys=True))
        return 1
    record = make_record(
        scope=args.scope,
        scope_key=scope_key,
        topic=args.topic,
        subject=" ".join(text.lower().split())[:120],
        text=text,
        tags=args.tag,
        trust="user",
        source_refs=(SourceRef(run_id="cli"),),
    )
    store.append(record)
    print(
        json.dumps(
            {"status": "written", "id": record.id, "scope": record.scope, "trust": record.trust},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0

