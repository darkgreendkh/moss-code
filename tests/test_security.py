from pathlib import Path

from moss.execution.safety.secrets import (
    REDACTED_VALUE,
    detected_secret_env_items,
    looks_sensitive_env_name,
    redact_artifact,
    redact_secret_shapes,
    shell_env,
)


def test_sensitive_env_name_detection_matches_runtime_policy():
    assert looks_sensitive_env_name("OPENAI_API_KEY")
    assert looks_sensitive_env_name("SERVICE_TOKEN")
    assert looks_sensitive_env_name("PASSWORD")
    assert not looks_sensitive_env_name("PATH")


def test_detected_secret_env_items_include_configured_and_sensitive_names():
    env = {
        "PATH": "/bin",
        "CUSTOM_SECRET_NAME": "custom-value",
        "OPENAI_API_KEY": "api-value",
    }

    items = detected_secret_env_items(env=env, secret_env_names={"CUSTOM_SECRET_NAME"})

    assert items == [("CUSTOM_SECRET_NAME", "custom-value"), ("OPENAI_API_KEY", "api-value")]


def test_redact_artifact_recurses_through_values_and_secret_keys():
    artifact = {
        "OPENAI_API_KEY": "api-value",
        "payload": ["api-value", {"nested": "custom-value"}],
    }

    redacted = redact_artifact(
        artifact,
        env={"OPENAI_API_KEY": "api-value", "CUSTOM_SECRET_NAME": "custom-value"},
        secret_env_names={"CUSTOM_SECRET_NAME"},
    )

    assert redacted["OPENAI_API_KEY"] == REDACTED_VALUE
    assert redacted["payload"] == [REDACTED_VALUE, {"nested": REDACTED_VALUE}]


def test_shell_env_uses_allowlist_and_sets_pwd_with_path_fallback(tmp_path):
    env = {"PATH": "/usr/bin", "HOME": "/home/user", "SECRET": "nope"}

    filtered = shell_env(env=env, allowlist=("HOME",), root=tmp_path)

    assert filtered == {"HOME": "/home/user", "PWD": str(tmp_path), "PATH": "/usr/bin"}


def test_shape_based_redaction_catches_secrets_that_are_not_in_the_env():
    """只替换环境变量的值不够：agent 会**读到**仓库里的密钥。"""
    for text in (
        "key = sk-abcdefghijklmnop1234",
        "token: ghp_abcdefghijklmnopqrst12",
        "AKIAIOSFODNN7EXAMPLE",
        "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",
        'api_key: "supersecretvalue123"',
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    ):
        assert REDACTED_VALUE in redact_secret_shapes(text), text


def test_shape_based_redaction_leaves_ordinary_text_alone():
    for text in ("normal text with no secrets", "short token = abc", "def main(): pass"):
        assert redact_secret_shapes(text) == text


def test_reading_a_secret_from_the_workspace_never_reaches_the_artifacts(tmp_path):
    from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / "config.ini").write_text("api_key = sk-live-abcdefghijklmnop123456\n", encoding="utf-8")
    agent = Moss(
        model_client=FakeModelClient(['<tool>{"name":"read_file","args":{"path":"config.ini"}}</tool>', "<final>Done.</final>"]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )

    agent.ask("read the config")

    run_dir = agent.run_store.run_dir(agent.current_task_state)
    artifacts = [
        (run_dir / "trace.jsonl").read_text(encoding="utf-8"),
        (run_dir / "report.json").read_text(encoding="utf-8"),
        # session v2 是目录：meta + history + checkpoints 都要查。
        "".join(item.read_text(encoding="utf-8") for item in sorted(Path(agent.session_path).iterdir())),
    ]
    for text in artifacts:
        assert "sk-live-abcdefghijklmnop123456" not in text
