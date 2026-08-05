"""一轮输出里的多个动作（spec-02 §4.1）。"""

from moss.output_parser import parse_model_actions, parse_model_output, truncate_after_final


def test_single_tool_output_degrades_to_the_legacy_result():
    """兼容硬约束：单动作时必须和 parse_model_output 完全一致。"""
    raw = '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>'

    actions = parse_model_actions(raw)
    kind, payload = parse_model_output(raw)

    assert len(actions) == 1
    assert actions[0].kind == kind == "tool"
    assert actions[0].name == payload["name"]
    assert actions[0].args == payload["args"]


def test_multiple_tool_blocks_keep_their_written_order():
    raw = (
        '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>\n'
        '<tool>{"name":"read_file","args":{"path":"b.py"}}</tool>\n'
        '<tool>{"name":"read_file","args":{"path":"c.py"}}</tool>'
    )

    actions = parse_model_actions(raw)

    assert [action.index for action in actions] == [0, 1, 2]
    assert [action.args["path"] for action in actions] == ["a.py", "b.py", "c.py"]


def test_final_truncates_the_actions_after_it():
    raw = (
        '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>\n'
        "<final>All done.</final>\n"
        '<tool>{"name":"write_file","args":{"path":"b.py","content":"x"}}</tool>'
    )

    kept, dropped = truncate_after_final(parse_model_actions(raw))

    assert [action.kind for action in kept] == ["tool", "final"]
    assert kept[1].text == "All done."
    assert [action.name for action in dropped] == ["write_file"]


def test_one_malformed_block_does_not_kill_the_rest():
    raw = (
        '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>\n'
        "<tool>{not json}</tool>\n"
        '<tool>{"name":"read_file","args":{"path":"c.py"}}</tool>'
    )

    actions = parse_model_actions(raw)

    assert [action.kind for action in actions] == ["tool", "retry", "tool"]
    assert "malformed tool JSON" in actions[1].text


def test_xml_style_blocks_mix_with_json_blocks():
    raw = (
        '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>\n'
        '<tool name="write_file" path="b.py"><content>hello\nworld\n</content></tool>'
    )

    actions = parse_model_actions(raw)

    assert [action.name for action in actions] == ["read_file", "write_file"]
    assert actions[1].args["content"] == "hello\nworld\n"


def test_call_id_is_preserved_and_kept_out_of_tool_args():
    raw = (
        '<tool>{"name":"read_file","args":{"path":"a.py"},"call_id":"toolu_1"}</tool>\n'
        '<tool name="read_file" path="b.py" call_id="toolu_2"></tool>'
    )

    actions = parse_model_actions(raw)

    assert [action.call_id for action in actions] == ["toolu_1", "toolu_2"]
    # call_id 是协议字段，混进 args 会让工具校验因为多了未知字段而失败。
    assert "call_id" not in actions[1].args


def test_empty_final_block_becomes_a_retry():
    raw = '<tool>{"name":"read_file","args":{"path":"a.py"}}</tool>\n<final></final>'

    actions = parse_model_actions(raw)

    assert actions[1].kind == "retry"


def test_bare_text_still_becomes_a_single_final():
    actions = parse_model_actions("just an answer")

    assert len(actions) == 1
    assert actions[0].kind == "final"
    assert actions[0].text == "just an answer"
