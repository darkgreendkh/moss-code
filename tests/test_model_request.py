import importlib

from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext


def test_flatten_joins_system_and_message_blocks_in_stable_order():
    model_request = importlib.import_module("moss.model_request")
    request = model_request.ModelRequest(
        system=(
            model_request.Block("IDENTITY", kind="identity"),
            model_request.Block("RULES", kind="rules"),
        ),
        messages=(
            model_request.Message("user", (model_request.Block("WORKSPACE", kind="workspace"),)),
            model_request.Message("assistant", (model_request.Block("HISTORY", kind="history"),)),
            model_request.Message(
                "user",
                (model_request.Block("REQUEST", kind="request", trust="user"),),
            ),
        ),
    )

    assert request.flatten() == "IDENTITY\n\nRULES\n\nWORKSPACE\n\nHISTORY\n\nREQUEST"


def test_context_bundle_flatten_matches_legacy_prompt_byte_for_byte(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )
    agent.record({"role": "assistant", "content": "earlier", "created_at": "2026-08-05T00:00:00Z"})

    legacy_text, legacy_metadata = agent.context_manager.build("do it")
    bundle = agent.context_manager.build_bundle("do it")

    assert bundle.text == legacy_text
    assert bundle.request.flatten() == legacy_text
    assert bundle.metadata == legacy_metadata


class StructuredCapturingClient(FakeModelClient):
    def __init__(self):
        super().__init__(["<final>structured</final>"])
        self.supports_prompt_cache = True
        self.requests = []

    def complete_request(self, request):
        self.requests.append(request)
        return super().complete_request(request)


def test_agent_loop_prefers_complete_request_without_changing_flat_prompt(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    client = StructuredCapturingClient()
    agent = Moss(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )

    assert agent.ask("hello") == "structured"
    assert len(client.requests) == 1
    assert client.prompts == [client.requests[0].flatten()]
    assert client.requests[0].cache_key == agent.last_prompt_metadata["prompt_cache_key"]
