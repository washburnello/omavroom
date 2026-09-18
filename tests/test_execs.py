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
