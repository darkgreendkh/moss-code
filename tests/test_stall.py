"""停滞检测（spec-02 §4.5）。"""

from moss.stall import args_digest, detect_stall, is_repeated_call


def _event(name, path="a.py", changed=False, error=""):
    return {
        "name": name,
        "args": {"path": path},
        "workspace_changed": changed,
        "tool_error_code": error,
    }


def test_repeat_exact_needs_three_identical_calls():
    events = [_event("read_file")] * 3

    signal = detect_stall(events)

    assert signal.kind == "repeat_exact"
    assert "read_file" in signal.detail


def test_two_identical_calls_are_not_yet_a_stall():
    """两次重复还可能是正常的重试，三次才是环。"""
    events = [_event("read_file"), _event("read_file")]

    assert detect_stall(events) is None


def test_ab_loop_detects_alternating_calls():
    events = [
        _event("read_file", "a.py", changed=True),
        _event("run_shell", "b.py", changed=True),
        _event("read_file", "a.py", changed=True),
        _event("run_shell", "b.py", changed=True),
    ]

    signal = detect_stall(events)

    assert signal.kind == "ab_loop"
    assert "read_file" in signal.detail and "run_shell" in signal.detail


def test_three_gram_loop_is_detected():
    cycle = [
        _event("read_file", "a.py", changed=True),
        _event("edit_file", "b.py", changed=True),
        _event("run_shell", "c.py", changed=True),
    ]

    signal = detect_stall(cycle * 2)

    assert signal.kind == "ab_loop"


def test_no_progress_after_four_unchanged_steps_without_new_paths():
    events = [
        _event("read_file", "a.py"),
        _event("read_file", "b.py"),
        _event("list_files", "a.py"),
        _event("list_files", "b.py"),
        _event("read_file", "a.py"),
        _event("list_files", "b.py"),
    ]

    signal = detect_stall(events)

    assert signal.kind in {"no_progress", "repeat_exact"}


def test_reading_new_files_is_progress_not_a_stall():
    """调研阶段本来就不改工作区，只要还在读到新文件就不算卡住。"""
    events = [_event("read_file", f"file{index}.py") for index in range(6)]

    assert detect_stall(events) is None


def test_error_storm_needs_the_same_error_three_times_in_a_row():
    events = [
        _event("run_shell", changed=True, error="tool_failed"),
        _event("run_shell", changed=True, error="tool_failed"),
        _event("run_shell", changed=True, error="tool_failed"),
    ]

    signal = detect_stall(events)

    assert signal.kind in {"repeat_exact", "error_storm"}


def test_error_storm_with_distinct_calls_is_reported_as_error_storm():
    events = [
        _event("run_shell", f"{index}.py", changed=True, error="tool_failed")
        for index in range(3)
    ]

    signal = detect_stall(events)

    assert signal.kind == "error_storm"
    assert "tool_failed" in signal.detail


def test_different_errors_in_a_row_are_not_a_storm():
    events = [
        _event("run_shell", "0.py", changed=True, error="tool_failed"),
        _event("run_shell", "1.py", changed=True, error="invalid_arguments"),
        _event("run_shell", "2.py", changed=True, error="tool_failed"),
    ]

    assert detect_stall(events) is None


def test_healthy_run_triggers_nothing():
    events = [
        _event("read_file", "a.py"),
        _event("edit_file", "a.py", changed=True),
        _event("run_shell", "test", changed=True),
        _event("read_file", "b.py"),
    ]

    assert detect_stall(events) is None


def test_empty_history_is_not_a_stall():
    assert detect_stall([]) is None


def test_window_bounds_how_far_back_detection_looks():
    old = [_event("read_file", "a.py")] * 3
    recent = [_event("read_file", f"{index}.py", changed=True) for index in range(4)]

    assert detect_stall(old + recent, window=4) is None


def test_notice_names_the_kind_and_offers_a_way_out():
    signal = detect_stall([_event("read_file")] * 3)

    notice = signal.notice()

    assert "repeat_exact" in notice
    # 干预必须给出路，而不只是"别这么干"。
    assert "<final>" in notice


def test_args_digest_ignores_key_order():
    assert args_digest({"a": 1, "b": 2}) == args_digest({"b": 2, "a": 1})


def test_args_digest_survives_unserializable_values():
    assert args_digest({"path": object()})


def test_is_repeated_call_keeps_the_legacy_two_in_a_row_rule():
    events = [_event("read_file"), _event("read_file")]

    assert is_repeated_call(events, "read_file", {"path": "a.py"}) is True
    assert is_repeated_call(events, "read_file", {"path": "other.py"}) is False
    assert is_repeated_call(events[:1], "read_file", {"path": "a.py"}) is False
