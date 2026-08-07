"""上手与发现性：无 key 引导、--help 分组、示例。"""

from moss.cli.parser import build_arg_parser
from moss.cli.repl import HELP_DETAILS, render_missing_key_hint


def test_missing_key_hint_is_actionable():
    hint = render_missing_key_hint("deepseek")
    # 指出填哪个变量、给出无 key 的退路。
    assert "MOSS_DEEPSEEK_API_KEY" in hint
    assert ".env" in hint
    assert "ollama" in hint


def test_missing_key_hint_falls_back_for_unknown_provider():
    hint = render_missing_key_hint("mystery")
    assert "provider API key" in hint


def test_help_examples_present_in_repl_help():
    assert "Examples:" in HELP_DETAILS
    assert "/approval" in HELP_DETAILS
    assert "/config" in HELP_DETAILS


def test_arg_parser_groups_do_not_change_parsing():
    parser = build_arg_parser()
    # 分组只影响 --help 排版，解析行为必须逐字段不变。
    args = parser.parse_args(["--approval", "auto", "--max-steps", "7", "hello", "world"])
    assert args.approval == "auto"
    assert args.max_steps == 7
    assert args.prompt == ["hello", "world"]
    # 默认值保持不变。
    defaults = parser.parse_args([])
    assert defaults.approval == "ask"
    assert defaults.sandbox == "auto"
    assert defaults.compaction == "off"
    assert defaults.replay_on_miss == "fail"


def test_arg_parser_help_has_named_groups(capsys):
    parser = build_arg_parser()
    text = parser.format_help()
    for group in ("connection:", "session:", "safety:", "control loop:", "extensions:"):
        assert group in text
