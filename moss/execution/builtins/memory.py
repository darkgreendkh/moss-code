"""Memory tool adapters."""

import json

def _json_result(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def tool_memory_write(context, args):
    return _json_result(context.write_memory(args))


def tool_memory_update(context, args):
    return _json_result(context.update_memory(args))


def tool_memory_delete(context, args):
    return _json_result(context.delete_memory(args))


def tool_memory_search(context, args):
    matches = context.search_memory(args)
    return _json_result(matches) if matches else "no relevant memory"

