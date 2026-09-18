"""Phase 4 exec tracker: bounds, kill, and lifecycle (FIX 6)."""

from __future__ import annotations

import pytest

from omavroom.manager.execs import ExecTracker


def test_duplicate_exec_id_rejected() -> None:
    tracker = ExecTracker()
    tracker.start(1, "e1")
    with pytest.raises(ValueError, match="duplicate"):
        tracker.start(1, "e1")


def test_empty_exec_id_rejected() -> None:
    tracker = ExecTracker()
    with pytest.raises(ValueError):
        tracker.start(1, "")


def test_unknown_poll_and_finish_raise_keyerror() -> None:
    tracker = ExecTracker()
    with pytest.raises(KeyError):
        tracker.poll(1, "nope")
    with pytest.raises(KeyError):
        tracker.finish(1, "nope")
    with pytest.raises(KeyError):
        tracker.kill(1, "nope")


def test_active_count_flips_on_finish_and_kill() -> None:
    tracker = ExecTracker()
    tracker.start(1, "e1")
    tracker.start(1, "e2")
    assert tracker.active_count(1) == 2
    view = tracker.finish(1, "e1", exit_code=0)
    assert view.state == "finished" and view.exit_code == 0
    assert tracker.active_count(1) == 1
    killed = tracker.kill(1, "e2")
    assert killed.state == "killed" and killed.exit_code == -9
    assert tracker.active_count(1) == 0


def test_terminate_is_idempotent_for_exit_code() -> None:
    tracker = ExecTracker()
    tracker.start(1, "e1")
    tracker.finish(1, "e1", exit_code=3)
    again = tracker.kill(1, "e1")
    assert again.state == "finished" and again.exit_code == 3


def test_max_concurrent_per_seat_enforced() -> None:
    tracker = ExecTracker(max_concurrent_per_seat=1)
    tracker.start(1, "e1")
    with pytest.raises(ValueError, match="running execs"):
        tracker.start(1, "e2")
    # A different seat is unaffected.
    tracker.start(2, "e1")


def test_history_is_bounded() -> None:
    tracker = ExecTracker(max_concurrent_per_seat=4, max_history_per_seat=2)
    for index in range(5):
        tracker.start(1, f"e{index}")
        tracker.finish(1, f"e{index}")
    views = tracker.list(1)
    assert len(views) == 2
    assert [view.exec_id for view in views] == ["e3", "e4"]


def test_running_execs_are_not_evicted_by_history_bound() -> None:
    tracker = ExecTracker(max_concurrent_per_seat=3, max_history_per_seat=1)
    tracker.start(1, "run1")
    tracker.start(1, "run2")
    assert tracker.active_count(1) == 2


# --------------------------------------------------------------------------
# Frozen Phase 5 exec contract: command/timeout in, stdout/stderr/exit_code/
# truncated out (ring buffer).
# --------------------------------------------------------------------------
def test_start_records_command_and_timeout() -> None:
    tracker = ExecTracker()
    view = tracker.start(1, "e1", label="build", command="make test", timeout_s=120)
    assert view.command == "make test"
    assert view.timeout_s == 120
    assert view.stdout == ""
    assert view.stderr == ""
    assert view.truncated is False


def test_poll_returns_output_fields() -> None:
    tracker = ExecTracker()
    tracker.start(1, "e1", command="echo hi")
    tracker.record_output(1, "e1", stdout="hi\n", stderr="warn\n")
    view = tracker.poll(1, "e1")
    assert view.stdout == "hi\n"
    assert view.stderr == "warn\n"
    assert view.exit_code is None
    assert view.truncated is False


def test_output_buffer_is_bounded_ring() -> None:
    tracker = ExecTracker(max_output_bytes=10)
    tracker.start(1, "e1")
    tracker.record_output(1, "e1", stdout="0123456789")
    tracker.record_output(1, "e1", stdout="abc")
    view = tracker.poll(1, "e1")
    assert view.stdout == "3456789abc"  # keeps the most recent 10 chars
    assert view.truncated is True


def test_finish_carries_final_output_and_exit_code() -> None:
    tracker = ExecTracker()
    tracker.start(1, "e1", command="true")
    view = tracker.finish(1, "e1", exit_code=0, stdout="done\n")
    assert view.state == "finished"
    assert view.exit_code == 0
    assert view.stdout == "done\n"


def test_record_output_unknown_exec_raises() -> None:
    tracker = ExecTracker()
    with pytest.raises(KeyError):
        tracker.record_output(1, "nope", stdout="x")
