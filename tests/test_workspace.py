import subprocess

from moss.context.repository.workspace import (
    WORKSPACE_FINGERPRINT_VERSION,
    WorkspaceContext,
    capture_snapshot,
    collect_git_facts,
    discover_docs,
    diff_snapshots,
    find_nearest_instruction_docs,
    invalidate_git_facts_cache,
    parse_status_counts,
)


def _init_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)


def test_capture_snapshot_records_files_and_skips_ignored_dirs(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("print(1)", encoding="utf-8")

    snapshot = capture_snapshot(tmp_path)

    assert set(snapshot) == {"a.txt", "pkg/b.py"}
    mtime_ns, size = snapshot["a.txt"]
    assert size == 5
    assert mtime_ns > 0


def test_diff_snapshots_reports_created_deleted_modified():
    before = {"a.txt": (1, 5), "b.txt": (1, 3), "c.txt": (1, 1)}
    after = {"a.txt": (2, 6), "c.txt": (1, 1), "d.txt": (1, 2)}

    changed, summaries = diff_snapshots(before, after)

    assert changed == ["a.txt", "b.txt", "d.txt"]
    assert summaries == ["modified:a.txt", "deleted:b.txt", "created:d.txt"]


def test_fingerprint_reacts_to_edits_past_the_preview_budget(tmp_path):
    """preview 只保留前 1200 字符，但指纹必须覆盖全文。

    否则 agent 改了长文档的尾部，prompt 缓存不会失效，
    模型继续拿着过期的 README 干活。
    """
    (tmp_path / "README.md").write_text("a" * 3000, encoding="utf-8")
    before = WorkspaceContext.build(tmp_path).fingerprint()

    (tmp_path / "README.md").write_text("a" * 2000 + "b" + "a" * 999, encoding="utf-8")
    after = WorkspaceContext.build(tmp_path).fingerprint()

    assert before != after
    assert before.startswith(f"{WORKSPACE_FINGERPRINT_VERSION}:")


def test_fingerprint_carries_a_version_prefix(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    fingerprint = WorkspaceContext.build(tmp_path).fingerprint()

    version, _, digest = fingerprint.partition(":")
    assert version == WORKSPACE_FINGERPRINT_VERSION
    assert len(digest) == 64


def test_build_from_subdirectory_keeps_cwd_and_fingerprint_stable(tmp_path):
    """在子目录启动时，重复 build 不能让 cwd 退化成 repo_root。"""
    _init_repo(tmp_path)
    nested = tmp_path / "pkg"
    nested.mkdir()

    first = WorkspaceContext.build(nested, repo_root_override=tmp_path)
    second = WorkspaceContext.build(first.invocation_cwd, repo_root_override=tmp_path)

    assert first.invocation_cwd == second.invocation_cwd
    assert first.repo_root == second.repo_root
    assert first.invocation_cwd != first.repo_root
    assert first.fingerprint() == second.fingerprint()


def test_collect_git_facts_runs_two_subprocesses(tmp_path, monkeypatch):
    """稳态采集只允许 status 与 log 两个子进程。"""
    _init_repo(tmp_path)
    invalidate_git_facts_cache()
    # 先预热仓库身份缓存（rev-parse / symbolic-ref 只在 cache miss 时跑）。
    collect_git_facts(tmp_path)
    invalidate_git_facts_cache()

    calls = []
    real_run = subprocess.run

    def counting_run(args, **kwargs):
        calls.append(list(args))
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    facts = collect_git_facts(tmp_path)

    assert len(calls) == 2
    assert [args[1] for args in calls] == ["status", "log"]
    assert facts.branch in {"main", "master"}


def test_collect_git_facts_reuses_cache_within_ttl(tmp_path):
    _init_repo(tmp_path)
    invalidate_git_facts_cache()

    first = collect_git_facts(tmp_path)
    second = collect_git_facts(tmp_path)

    assert first is second


def test_parse_status_counts_classifies_each_status_code():
    entries = [" M a.py", "A  b.py", " D c.py", "?? d.py", "R  e.py -> f.py"]

    assert parse_status_counts(entries) == {
        "modified": 2,
        "added": 1,
        "deleted": 1,
        "untracked": 1,
    }


def test_status_text_leads_with_counts_and_caps_entries(tmp_path):
    entries = tuple(f" M file{index:03d}.py" for index in range(25))
    workspace = WorkspaceContext(
        cwd="/repo",
        repo_root="/repo",
        branch="main",
        default_branch="main",
        status="\n".join(entries),
        recent_commits=[],
        project_docs={},
        status_entries=entries,
    )

    rendered = workspace.status_text().splitlines()

    assert rendered[0] == "modified: 25"
    assert len(rendered) == 1 + 20 + 1
    assert rendered[-1] == "… 5 more"


def test_status_text_reports_clean_workspace():
    workspace = WorkspaceContext(
        cwd="/repo",
        repo_root="/repo",
        branch="main",
        default_branch="main",
        status="clean",
        recent_commits=[],
        project_docs={},
    )

    assert workspace.status_text() == "clean"


def test_oversized_doc_becomes_a_structural_summary(tmp_path):
    """超预算的文档退化成标题树 + 开头 + 取回指针，而不是硬切一刀。"""
    body = "\n".join(["# Title", "intro line", "## Setup", "x" * 400, "## Deploy", "y" * 1200])
    (tmp_path / "README.md").write_text(body, encoding="utf-8")

    ref = WorkspaceContext.build(tmp_path).doc_refs["README.md"]

    assert ref.truncated is True
    assert "# Title" in ref.preview
    assert "## Deploy" in ref.preview
    assert "read_file README.md" in ref.preview
    assert ref.total_lines == len(body.splitlines())


def test_small_doc_is_kept_verbatim(tmp_path):
    (tmp_path / "README.md").write_text("# Title\nshort\n", encoding="utf-8")

    ref = WorkspaceContext.build(tmp_path).doc_refs["README.md"]

    assert ref.truncated is False
    assert ref.preview == "# Title\nshort\n"


def test_doc_discovery_covers_the_layered_default_names(tmp_path):
    for name in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "Makefile", "justfile"):
        (tmp_path / name).write_text(f"# {name}\n", encoding="utf-8")

    refs = discover_docs(tmp_path, tmp_path)

    assert {"AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "Makefile", "justfile"} <= set(refs)


def test_project_config_can_override_the_doc_names(tmp_path):
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp_path / "HACKING.md").write_text("hacking\n", encoding="utf-8")
    (tmp_path / ".moss").mkdir()
    (tmp_path / ".moss" / "config.json").write_text(
        '{"repo_context": {"doc_names": ["HACKING.md"]}}', encoding="utf-8"
    )

    docs = WorkspaceContext.build(tmp_path).project_docs

    assert set(docs) == {"HACKING.md"}


def test_find_nearest_instruction_docs_orders_near_to_far(tmp_path):
    (tmp_path / "AGENTS.md").write_text("root rules\n", encoding="utf-8")
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)
    (tmp_path / "pkg" / "AGENTS.md").write_text("pkg rules\n", encoding="utf-8")
    (nested / "mod.py").write_text("x = 1\n", encoding="utf-8")

    found = find_nearest_instruction_docs(tmp_path, nested / "mod.py")

    assert found == ["pkg/AGENTS.md", "AGENTS.md"]
