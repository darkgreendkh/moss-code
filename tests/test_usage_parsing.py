from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext
from moss.providers import clients


def test_openai_usage_exposes_unified_cache_telemetry():
    metadata = clients.parse_openai_usage(
        {
            "usage": {
                "input_tokens": 2048,
                "output_tokens": 32,
                "total_tokens": 2080,
                "input_tokens_details": {"cached_tokens": 1536},
            }
        }
    )

    assert metadata == {
        "input_tokens": 2048,
        "output_tokens": 32,
        "total_tokens": 2080,
        "cache_read_tokens": 1536,
        "cache_write_tokens": 0,
        "cache_metrics_available": True,
        "cached_tokens": 1536,
        "cache_hit": True,
    }


def test_anthropic_usage_exposes_creation_and_read_tokens():
    metadata = clients.parse_anthropic_usage(
        {
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 80,
                "cache_creation_input_tokens": 700,
                "cache_read_input_tokens": 450,
            }
        }
    )

    assert metadata == {
        "input_tokens": 1200,
        "output_tokens": 80,
        "total_tokens": None,
        "cache_read_tokens": 450,
        "cache_write_tokens": 700,
        "cache_metrics_available": True,
        "cached_tokens": 450,
        "cache_hit": True,
    }


def test_missing_cache_fields_are_not_reported_as_zero_percent():
    metadata = clients.parse_anthropic_usage({"usage": {"input_tokens": 10, "output_tokens": 2}})

    assert metadata["cache_metrics_available"] is False
    assert metadata["cache_read_tokens"] == 0
    assert metadata["cache_write_tokens"] == 0


class CacheCapturingClient(FakeModelClient):
    supports_prompt_cache = True

    def __init__(self):
        super().__init__(["<final>ok</final>"])
        self.supports_prompt_cache = True
        self.kwargs = None

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.kwargs = dict(kwargs)
        return super().complete(prompt, max_new_tokens, **kwargs)


def test_prompt_cache_feature_flag_suppresses_provider_cache_arguments(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    client = CacheCapturingClient()
    agent = Moss(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
        feature_flags={"prompt_cache": False},
    )

    assert agent.ask("hello") == "ok"
    assert client.kwargs["prompt_cache_key"] is None
    assert client.kwargs["prompt_cache_retention"] is None
