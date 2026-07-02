import os

from moss.config import find_project_env, load_project_env, provider_env


def test_load_project_env_skips_malformed_lines_without_crashing(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "GOOD_ONE=value1",
                "this line has no equals sign and used to crash startup",
                "# a comment",
                "GOOD_TWO=value2",
                "1INVALID_NAME=nope",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("GOOD_ONE", raising=False)
    monkeypatch.delenv("GOOD_TWO", raising=False)

    loaded = load_project_env(tmp_path)

    # 合法配置照常生效，坏行被跳过，不会抛异常。
    assert loaded["GOOD_ONE"] == "value1"
    assert loaded["GOOD_TWO"] == "value2"
    assert "this line has no equals sign and used to crash startup" not in loaded
    assert os.environ["GOOD_ONE"] == "value1"
    assert os.environ["GOOD_TWO"] == "value2"

    # 坏行会给出带行号的警告，方便定位。
    warnings = capsys.readouterr().err
    assert "skipping .env:2" in warnings
    assert "skipping .env:5" in warnings


def test_load_project_env_returns_empty_when_no_env_file(tmp_path):
    assert load_project_env(tmp_path) == {}
    assert find_project_env(tmp_path) is None


def test_provider_env_prefers_primary_then_legacy(monkeypatch):
    monkeypatch.delenv("PRIMARY", raising=False)
    monkeypatch.setenv("LEGACY", "legacy-value")

    assert provider_env("PRIMARY", ("LEGACY",), "fallback") == "legacy-value"
    assert provider_env("MISSING", (), "fallback") == "fallback"
