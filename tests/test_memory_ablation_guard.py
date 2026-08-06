import pytest

from moss.evaluation.ablations import assert_fact_absent, robust_fact_match


def test_critical_fact_in_any_nested_prompt_section_invalidates_trial():
    prompt = {
        "system": "Use prior memory only when relevant.",
        "history": [{"role": "assistant", "content": "Database—is SQLITE!"}],
        "task": "Which database should we use?",
    }

    with pytest.raises(ValueError, match="self-proving"):
        assert_fact_absent(prompt, "database is sqlite")


def test_fact_guard_accepts_prompt_without_fact_and_matcher_is_format_robust():
    assert_fact_absent({"task": "Which database should we use?"}, "database is sqlite")

    assert robust_fact_match("Use `SQLite` as the DATABASE.", "database sqlite") is True

