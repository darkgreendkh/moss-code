"""Pure grouping helpers for provider-native tool history."""

import json

from .model_request import Block, Message


def native_tool_turn(group, entries, result_positions, consumed, *, clip, missing_result):
    """Convert one adjacent call group into a balanced call/result message pair."""
    call_blocks = []
    result_blocks = []
    for item in group:
        call_id = str(item.get("call_id", "") or "")
        call_blocks.append(
            Block(
                # 不转义非 ASCII：这串是发给模型的 tool_use 入参，
                # 中文写成 \uXXXX 既让模型读到的是转义码，一个汉字还要占 6 个字符的预算。
                json.dumps(item.get("args", {}), separators=(",", ":"), sort_keys=True, ensure_ascii=False),
                kind="tool_call",
                source=str(item.get("name", "")),
                trust="model",
                call_id=call_id,
            )
        )
        position = result_positions.get(call_id)
        if position is None or position in consumed:
            content = missing_result
            source = str(item.get("name", ""))
        else:
            consumed.add(position)
            result = entries[position]
            content = clip(str(result.get("content", "")), 900, keep="head")
            source = str(result.get("name", "")) or str(item.get("name", ""))
        result_blocks.append(
            Block(content, kind="tool_result", source=source, trust="tool", call_id=call_id)
        )
    single = str(group[0].get("call_id", "") or "") if len(group) == 1 else None
    return [
        Message(role="assistant", blocks=tuple(call_blocks), call_id=single),
        Message(role="tool", blocks=tuple(result_blocks), call_id=single),
    ]
