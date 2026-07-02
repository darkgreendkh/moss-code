from moss.workspace import capture_snapshot, diff_snapshots


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
