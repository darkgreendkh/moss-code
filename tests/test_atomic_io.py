"""原子 + 持久落盘的行为（spec-07 §4.2）。

这些测试守的是一句承诺：注释里写着"断电不丢"的地方，真的调用了 fsync。
"""

import json
import os

import pytest

from moss import atomic_io


@pytest.fixture(autouse=True)
def _clean_degradations():
    atomic_io.reset_degradations()
    yield
    atomic_io.reset_degradations()


def test_write_atomic_fsyncs_file_and_directory(tmp_path, monkeypatch):
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])

    path = atomic_io.write_atomic(tmp_path / "a.txt", "hello")

    # 一次是临时文件的内容，一次是目录项（rename 的持久化靠后者）。
    assert len(calls) == 2
    assert path.read_text(encoding="utf-8") == "hello"


def test_write_atomic_leaves_no_temp_files(tmp_path):
    atomic_io.write_atomic(tmp_path / "a.txt", "one")
    atomic_io.write_atomic(tmp_path / "a.txt", "two")

    assert sorted(item.name for item in tmp_path.iterdir()) == ["a.txt"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "two"


def test_write_atomic_cleans_up_temp_file_when_write_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("boom")))

    with pytest.raises(OSError):
        atomic_io.write_atomic(tmp_path / "a.txt", "hello")

    assert list(tmp_path.iterdir()) == []


def test_directory_fsync_failure_is_swallowed_and_recorded(tmp_path, monkeypatch):
    """Windows 打不开目录 fd。降级要显式：写照常成功，但记一条降级。"""
    original_open = os.open

    def fake_open(path, flags, *args, **kwargs):
        if os.path.isdir(path):
            raise PermissionError(13, "Permission denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)
    messages = []

    class Stream:
        def write(self, text):
            messages.append(text)

        def flush(self):
            pass

    monkeypatch.setattr(atomic_io.sys, "stderr", Stream())

    path = atomic_io.write_atomic(tmp_path / "a.txt", "hello")

    assert path.read_text(encoding="utf-8") == "hello"
    names = [item["name"] for item in atomic_io.degradations()]
    assert names == ["dir_fsync_unsupported"]
    assert len(messages) == 1


def test_degradation_is_reported_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(atomic_io, "fsync_dir", lambda path: False)
    messages = []
    stream = type("S", (), {"write": lambda self, text: messages.append(text), "flush": lambda self: None})()

    atomic_io.note_degradation("thing", "detail", stream=stream)
    atomic_io.note_degradation("thing", "other detail", stream=stream)

    assert messages == ["warning: durability degraded (thing): detail\n"]
    assert atomic_io.degradations() == [{"name": "thing", "detail": "detail"}]


def test_write_json_atomic_round_trips(tmp_path):
    path = atomic_io.write_json_atomic(tmp_path / "a.json", {"b": 1, "a": "中文"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"a": "中文", "b": 1}


def test_append_line_fsyncs_every_n_lines(tmp_path, monkeypatch):
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    path = tmp_path / "trace.jsonl"

    for index in range(10):
        atomic_io.append_line(path, json.dumps({"i": index}), fsync_every=5)

    # 每 5 条一次，而不是每条一次——这就是文档里写明的取舍。
    assert len(calls) == 2
    assert len(path.read_text(encoding="utf-8").splitlines()) == 10


def test_append_line_force_fsync_settles_the_interval(tmp_path, monkeypatch):
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    path = tmp_path / "trace.jsonl"

    atomic_io.append_line(path, "a", fsync_every=100)
    assert calls == []
    atomic_io.append_line(path, "b", fsync_every=100, force_fsync=True)

    assert len(calls) == 1


def test_truncate_partial_tail_drops_the_unfinished_record(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n{"a": ', encoding="utf-8")

    dropped = atomic_io.truncate_partial_tail(path)

    assert dropped == len('{"a": ')
    assert path.read_text(encoding="utf-8") == '{"a": 1}\n{"a": 2}\n'
    assert [item["name"] for item in atomic_io.degradations()] == ["partial_jsonl_tail_dropped"]


def test_truncate_partial_tail_is_a_no_op_on_clean_files(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{"a": 1}\n', encoding="utf-8")

    assert atomic_io.truncate_partial_tail(path) == 0
    assert atomic_io.truncate_partial_tail(tmp_path / "missing.jsonl") == 0
    assert path.read_text(encoding="utf-8") == '{"a": 1}\n'


def test_truncate_partial_tail_handles_a_file_with_no_complete_record(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{"a": ', encoding="utf-8")

    atomic_io.truncate_partial_tail(path)

    assert path.read_text(encoding="utf-8") == ""


def test_read_last_line_returns_last_non_empty_line(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")

    assert atomic_io.read_last_line(path) == '{"a": 2}'


def test_read_last_line_handles_long_lines_and_missing_files(tmp_path):
    path = tmp_path / "trace.jsonl"
    long_line = json.dumps({"payload": "x" * 20000})
    path.write_text('{"a": 1}\n' + long_line + "\n", encoding="utf-8")

    assert atomic_io.read_last_line(path) == long_line
    assert atomic_io.read_last_line(tmp_path / "missing.jsonl") == ""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert atomic_io.read_last_line(empty) == ""


def test_read_last_line_handles_single_line_without_newline(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{"a": 1}', encoding="utf-8")

    assert atomic_io.read_last_line(path) == '{"a": 1}'
