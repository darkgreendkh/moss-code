from moss.output_parser import parse_model_output, parse_xml_tool, retry_notice


def test_parse_json_tool_call():
    kind, payload = parse_model_output('<tool>{"name": "read_file", "args": {"path": "a.py"}}</tool>')
    assert kind == "tool"
    assert payload == {"name": "read_file", "args": {"path": "a.py"}}


def test_parse_json_tool_call_with_null_args_defaults_to_empty_dict():
    kind, payload = parse_model_output('<tool>{"name": "list_files", "args": null}</tool>')
    assert kind == "tool"
    assert payload["args"] == {}


def test_parse_malformed_tool_json_returns_retry():
    kind, payload = parse_model_output("<tool>{not json}</tool>")
    assert kind == "retry"
    assert "malformed tool JSON" in payload


def test_parse_missing_tool_name_returns_retry():
    kind, payload = parse_model_output('<tool>{"args": {}}</tool>')
    assert kind == "retry"
    assert "missing a tool name" in payload


def test_parse_xml_style_tool_with_attributes_and_body():
    raw = '<tool name="write_file" path="a.py"><content>print(1)\nprint(2)</content></tool>'
    kind, payload = parse_model_output(raw)
    assert kind == "tool"
    assert payload == {"name": "write_file", "args": {"path": "a.py", "content": "print(1)\nprint(2)"}}


def test_parse_final_answer():
    assert parse_model_output("<final>done</final>") == ("final", "done")


def test_parse_bare_text_is_final_answer():
    assert parse_model_output("just text") == ("final", "just text")


def test_parse_empty_output_returns_retry():
    kind, payload = parse_model_output("   ")
    assert kind == "retry"
    assert "empty response" in payload


def test_tool_before_final_wins():
    raw = '<tool>{"name": "read_file", "args": {"path": "a.py"}}</tool><final>ignored</final>'
    kind, _ = parse_model_output(raw)
    assert kind == "tool"


def test_retry_notice_mentions_problem():
    assert "empty <final>" in retry_notice("model returned an empty <final> answer")


def test_parse_xml_tool_without_name_returns_none():
    assert parse_xml_tool('<tool path="a.py"></tool>') is None
