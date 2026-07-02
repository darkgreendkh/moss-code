from moss.token_budget import MAX_TOOL_OUTPUT, clip, clip_to_budget, estimate_tokens, middle


def test_estimate_tokens_counts_cjk_denser_than_latin():
    latin = "a" * 40
    cjk = "中" * 40
    # 同样 40 个字符，中文的 token 估算应该明显高于英文，
    # 因为主流分词里 CJK 字符通常 1 字符 >= 1 token，而英文约 4 字符/token。
    assert estimate_tokens(cjk) > estimate_tokens(latin)
    assert estimate_tokens(latin) <= 12
    assert estimate_tokens(cjk) >= 40


def test_estimate_tokens_empty_and_nonempty_floor():
    assert estimate_tokens("") == 0
    assert estimate_tokens("x") >= 1


def test_clip_to_budget_respects_token_measure():
    text = "中" * 100
    clipped = clip_to_budget(text, 10, measure=estimate_tokens, keep="head")
    assert estimate_tokens(clipped) <= 10
    # 用字符数当预算会以为还能塞下更多，token 计量则真正约束住了规模。
    assert len(clipped) < len(text)


def test_clip_to_budget_keep_head_tail_middle():
    text = "HEAD-" + ("m" * 40) + "-TAIL"
    head = clip_to_budget(text, 12, measure=len, keep="head")
    tail = clip_to_budget(text, 12, measure=len, keep="tail")
    middle = clip_to_budget(text, 12, measure=len, keep="middle")

    assert head.startswith("HEAD-")
    assert len(head) <= 12
    assert tail.endswith("-TAIL")
    assert len(tail) <= 12
    assert middle.startswith("HEAD") and middle.endswith("TAIL")
    assert len(middle) <= 12


def test_clip_to_budget_returns_text_when_already_within_budget():
    assert clip_to_budget("short", 100, measure=len) == "short"


def test_clip_under_limit_is_unchanged():
    assert clip("short", limit=100) == "short"


def test_clip_keep_head_appends_truncation_note():
    result = clip("abcdef", limit=3)
    assert result.startswith("abc")
    assert "[truncated 3 chars]" in result


def test_clip_keep_middle_preserves_both_ends():
    text = "HEAD" + "x" * 100 + "TAIL"
    result = clip(text, limit=10, keep="middle")
    assert result.startswith("HEAD")
    assert result.endswith("TAIL")
    assert "[truncated" in result


def test_clip_default_limit_is_max_tool_output():
    assert clip("x" * (MAX_TOOL_OUTPUT + 10)).startswith("x" * 100)


def test_middle_collapses_newlines_and_keeps_ends():
    text = "start" + "\n" * 5 + "y" * 50 + "end"
    result = middle(text, 20)
    assert len(result) == 20
    assert result.startswith("start")
    assert result.endswith("end")
    assert "\n" not in result
