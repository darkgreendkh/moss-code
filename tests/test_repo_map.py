"""仓库地图（spec-01 §4.1）的行为测试。"""

from moss.context.repository import repo_map as repo_maplib
from moss.context.repository.ignore import IgnoreRules
from moss.context.repository.repo_map import (
    build_repo_map,
    compute_cache_key,
    extract_symbols,
    get_repo_map,
    rank_relevant_files,
    render_repo_map,
    tokenize,
)
from moss.context.token_budget import estimate_tokens


def _sample_repo(tmp_path):
    (tmp_path / "moss").mkdir()
    (tmp_path / "moss" / "cli.py").write_text(
        '"""命令行入口。"""\n\n\ndef main():\n    return 0\n', encoding="utf-8"
    )
    (tmp_path / "moss" / "core.py").write_text(
        "class Engine:\n    def start(self):\n        return 1\n\n\nasync def boot():\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return tmp_path


def test_syntax_errors_do_not_raise(tmp_path):
    """仓库里存在写坏的 .py 是常态（模型自己刚写坏的更常见）。"""
    broken = tmp_path / "broken.py"
    broken.write_text("def oops(:\n", encoding="utf-8")

    assert extract_symbols(broken) == ()
    assert build_repo_map(tmp_path).entries


def test_python_symbols_carry_kind_and_line_numbers(tmp_path):
    _sample_repo(tmp_path)

    symbols = {symbol.name: symbol for symbol in extract_symbols(tmp_path / "moss" / "core.py")}

    assert symbols["Engine"].kind == "class"
    assert symbols["Engine"].line_start == 1
    assert symbols["Engine"].line_end == 3
    assert symbols["Engine.start"].kind == "def"
    assert symbols["Engine.start"].line_start == 2
    assert symbols["boot"].kind == "async def"
    assert symbols["boot"].line_start == 6


def test_module_docstring_first_line_becomes_a_symbol(tmp_path):
    _sample_repo(tmp_path)

    symbols = extract_symbols(tmp_path / "moss" / "cli.py")

    assert symbols[0].kind == "module"
    assert symbols[0].name == "命令行入口。"


def test_non_python_files_use_line_prefix_matching(tmp_path):
    target = tmp_path / "server.go"
    target.write_text("package main\n\nfunc Serve() error {\n\treturn nil\n}\n", encoding="utf-8")

    symbols = {symbol.name: symbol.line_start for symbol in extract_symbols(target)}

    assert symbols["Serve"] == 3


def test_binary_files_are_skipped(tmp_path):
    target = tmp_path / "blob.py"
    target.write_bytes(b"\x00\x01\x02binary")

    assert extract_symbols(target) == ()


def test_render_stays_within_budget(tmp_path):
    _sample_repo(tmp_path)
    for index in range(60):
        (tmp_path / f"module{index}.py").write_text(
            "\n".join(f"def fn{item}():\n    return {item}\n" for item in range(20)), encoding="utf-8"
        )

    repo_map = build_repo_map(tmp_path, budget_tokens=200)
    rendered = render_repo_map(repo_map, 200)

    assert estimate_tokens(rendered) <= 200
    assert repo_map.truncated is True


def test_ordering_is_deterministic(tmp_path):
    _sample_repo(tmp_path)

    first = render_repo_map(build_repo_map(tmp_path), 800)
    second = render_repo_map(build_repo_map(tmp_path), 800)

    assert first == second


def test_entry_files_rank_first(tmp_path):
    _sample_repo(tmp_path)
    # 让一个非入口文件明显更大更新，确认入口优先级压过 size * recency。
    (tmp_path / "moss" / "core.py").write_text("x = 1\n" * 5000, encoding="utf-8")

    paths = [entry.path for entry in build_repo_map(tmp_path).entries]

    assert paths[0] == "moss/cli.py"


def test_ignored_paths_stay_out_of_the_map(tmp_path):
    _sample_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.py").write_text("x = 1\n", encoding="utf-8")

    repo_map = build_repo_map(tmp_path, ignore=IgnoreRules.load(tmp_path))

    assert not any(entry.path.startswith("build/") for entry in repo_map.entries)


def test_cache_hit_skips_the_rebuild(tmp_path, monkeypatch):
    _sample_repo(tmp_path)
    cache_dir = tmp_path / ".moss" / "cache"

    first = get_repo_map(tmp_path, cache_dir=cache_dir)

    def explode(*args, **kwargs):
        raise AssertionError("cache hit must not rebuild the map")

    monkeypatch.setattr(repo_maplib, "build_repo_map", explode)
    second = get_repo_map(tmp_path, cache_dir=cache_dir)

    assert second.cache_key == first.cache_key
    assert [entry.path for entry in second.entries] == [entry.path for entry in first.entries]


def test_cache_key_changes_when_a_file_is_added(tmp_path):
    _sample_repo(tmp_path)
    before = compute_cache_key(tmp_path)

    (tmp_path / "moss" / "extra.py").write_text("x = 1\n", encoding="utf-8")

    assert compute_cache_key(tmp_path) != before


def test_corrupt_cache_falls_back_to_rebuilding(tmp_path):
    _sample_repo(tmp_path)
    cache_dir = tmp_path / ".moss" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "repo_map.json").write_text("{ not json", encoding="utf-8")

    repo_map = get_repo_map(tmp_path, cache_dir=cache_dir)

    assert repo_map.entries


def test_repo_map_lands_in_the_prompt_prefix(tmp_path, monkeypatch):
    from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext

    _sample_repo(tmp_path)
    monkeypatch.delenv("MOSS_REPO_MAP", raising=False)
    agent = Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
    )

    assert "Repo map:" in agent.prefix
    assert "moss/cli.py" in agent.prefix
    # 地图属于 workspace 段，不能进 stable_hash 覆盖的稳定头，
    # 否则仓库结构一变就打掉 prompt 缓存。
    assert "Repo map:" not in agent.prefix.split("Workspace:")[0]


def test_repo_map_off_restores_the_previous_prefix(tmp_path, monkeypatch):
    from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext

    _sample_repo(tmp_path)
    monkeypatch.setenv("MOSS_REPO_MAP", "off")
    agent = Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
    )

    assert "Repo map:" not in agent.prefix


def test_rank_relevant_files_matches_paths_and_symbols(tmp_path):
    _sample_repo(tmp_path)
    repo_map = build_repo_map(tmp_path)

    assert rank_relevant_files(repo_map, "fix the engine start bug")[0] == "moss/core.py"
    assert rank_relevant_files(repo_map, "update the cli entry point")[0] == "moss/cli.py"


def test_rank_relevant_files_is_deterministic_and_bounded(tmp_path):
    _sample_repo(tmp_path)
    repo_map = build_repo_map(tmp_path)

    first = rank_relevant_files(repo_map, "engine cli", limit=1)
    second = rank_relevant_files(repo_map, "engine cli", limit=1)

    assert first == second
    assert len(first) == 1


def test_rank_relevant_files_returns_nothing_for_an_unrelated_query(tmp_path):
    _sample_repo(tmp_path)

    assert rank_relevant_files(build_repo_map(tmp_path), "kubernetes ingress") == []


def test_tokenize_splits_snake_and_camel_case():
    assert tokenize("build_repoMap") == ["build", "repo", "map"]


def test_anchor_line_lands_in_the_relevant_memory_section(tmp_path, monkeypatch):
    from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext

    _sample_repo(tmp_path)
    monkeypatch.delenv("MOSS_REPO_MAP", raising=False)
    agent = Moss(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
    )

    prompt = agent.prompt("fix the engine start bug")

    assert "Likely relevant files: moss/core.py" in prompt
    # 起点锚必须待在 relevant_memory 段：它每轮都会变，进 prefix 会打掉 prompt 缓存。
    assert "Likely relevant files" not in prompt.split("Relevant memory:")[0]


def test_anchor_miss_is_traced_when_the_model_reads_something_else(tmp_path, monkeypatch):
    import json

    from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext

    _sample_repo(tmp_path)
    monkeypatch.delenv("MOSS_REPO_MAP", raising=False)
    agent = Moss(
        model_client=FakeModelClient(
            [
                '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
                "<final>Done.</final>",
            ]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )

    assert agent.ask("fix the engine start bug") == "Done."

    events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    misses = [event for event in events if event["event"] == "anchor_miss"]
    assert len(misses) == 1
    assert misses[0]["path"] == "README.md"
    assert "moss/core.py" in misses[0]["anchors"]
