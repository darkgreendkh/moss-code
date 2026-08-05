import shutil
import subprocess

import pytest

from moss.ignore import IgnoreRules, matches_any_glob, parse_exclude_globs


def _rules(*patterns):
    return IgnoreRules.from_patterns(patterns)


def test_comment_and_blank_lines_are_skipped():
    rules = _rules("# a comment", "", "   ", "build/")

    assert rules.match("build", is_dir=True) is True
    assert rules.match("a comment") is False


def test_basename_pattern_matches_at_any_depth():
    rules = _rules("*.pyc")

    assert rules.match("a.pyc") is True
    assert rules.match("pkg/deep/a.pyc") is True
    assert rules.match("a.py") is False


def test_leading_slash_anchors_to_repo_root():
    rules = _rules("/build")

    assert rules.match("build") is True
    assert rules.match("pkg/build") is False


def test_trailing_slash_matches_directories_only():
    rules = _rules("dist/")

    assert rules.match("dist", is_dir=True) is True
    assert rules.match("dist", is_dir=False) is False


def test_children_of_an_ignored_directory_are_ignored():
    rules = _rules("node_modules/")

    assert rules.match("node_modules/react/index.js") is True


def test_negation_reincludes_a_file():
    rules = _rules("*.log", "!keep.log")

    assert rules.match("debug.log") is True
    assert rules.match("keep.log") is False


def test_last_matching_rule_wins():
    rules = _rules("!keep.log", "*.log")

    assert rules.match("keep.log") is True


def test_double_star_spans_directories():
    rules = _rules("docs/**/draft.md")

    assert rules.match("docs/draft.md") is True
    assert rules.match("docs/a/b/draft.md") is True
    assert rules.match("other/draft.md") is False


def test_question_mark_matches_one_character():
    rules = _rules("cache?.bin")

    assert rules.match("cache1.bin") is True
    assert rules.match("cache12.bin") is False


def test_character_class_patterns_are_skipped_with_a_warning(capsys):
    rules = _rules("tmp[0-9].txt")

    assert rules.match("tmp1.txt") is False
    assert "character classes" in capsys.readouterr().err


def test_load_merges_defaults_with_repo_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("secret/\n*.bak\n", encoding="utf-8")

    rules = IgnoreRules.load(tmp_path)

    assert rules.match("secret/key.txt") is True
    assert rules.match("notes.bak") is True
    assert rules.match(".git/config") is True
    assert rules.match("moss/cli.py") is False


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_matches_git_check_ignore_on_a_sample_tree(tmp_path):
    """与 git 自己的判断对拍，避免手写匹配器悄悄跑偏。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(
        "\n".join(["build/", "*.log", "!keep.log", "/root-only.txt", "docs/**/draft.md"]) + "\n",
        encoding="utf-8",
    )
    samples = [
        "build/out.o",
        "pkg/build/out.o",
        "debug.log",
        "keep.log",
        "root-only.txt",
        "pkg/root-only.txt",
        "docs/a/b/draft.md",
        "moss/cli.py",
    ]
    for rel in samples:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")

    rules = IgnoreRules.load(tmp_path, use_defaults=False)
    for rel in samples:
        expected = (
            subprocess.run(
                ["git", "check-ignore", "-q", rel], cwd=tmp_path, capture_output=True
            ).returncode
            == 0
        )
        assert rules.match(rel) is expected, rel


def test_parse_exclude_globs_trims_and_drops_empties():
    assert parse_exclude_globs(" a/*, ,b.txt ") == ("a/*", "b.txt")
    assert parse_exclude_globs(None) == ()


def test_matches_any_glob():
    assert matches_any_glob("pkg/a.py", ("pkg/*.py",)) is True
    assert matches_any_glob("pkg/a.py", ("other/*",)) is False
