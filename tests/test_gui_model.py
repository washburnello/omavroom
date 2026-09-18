"""Phase 8 pure view-model tests: slot planning/stability/labels, no Qt, no daemon.

These mirror the poolview stability contract and pin the GUI-specific
additions: terminal live-text derivation, thumbnail data-URI construction,
heartbeat text, and the responsive grid packing rule.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omavroom.config import Config
from omavroom.gui.viewmodel import (
    ATTENTION_ACTIONS,
    TERMINAL_IDLE,
    MonitorWall,
    choose_columns,
    grid_columns,
    heartbeat_text,
    plan_from_config,
    render_terminal_text,
    tile_column_span,
    tile_row_span,
    wall_layout,
)
from omavroom.poolview import NO_SIGNAL

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _config(*, desktop: int = 1, terminal: int = 2) -> Config:
    cfg = Config.default()
    cfg.seats["desktop"].max_seats = desktop
    cfg.seats["terminal"].max_seats = terminal
    return cfg


def _seat(seat_id, name, seat_type="terminal", state="ready", agent=None, **extra):
    seat = {
        "id": seat_id,
        "name": name,
        "seat_type": seat_type,
        "state": state,
        "vm_name": f"fake://{name}",
        "image": "omavroom-base",
        "agent_label": agent,
        "last_error": None,
        "attempts": 0,
        "lease_expires_at": None,
        "last_heartbeat": None,
        "pending_action": None,
        "needs_attention": False,
    }
    seat.update(extra)
    return seat


def _claimed(request_id, seat_id, agent, *, project=None, acquired="2026-01-01T11:30:00.000000Z"):
    return {
        "id": request_id,
        "agent_label": agent,
        "seat_type": "terminal",
        "project": project,
        "image": "omavroom-base",
        "status": "claimed",
        "position": 1,
        "seat_id": seat_id,
        "queue_ahead": 0,
        "created_at": "2026-01-01T11:29:00.000000Z",
        "updated_at": acquired,
        "seat": None,
        "lease": {
            "id": request_id,
            "seat_id": seat_id,
            "request_id": request_id,
            "agent_label": agent,
            "acquired_at": acquired,
            "expires_at": "2026-01-01T13:00:00.000000Z",
            "last_heartbeat": acquired,
        },
    }


def _status(*, seats, queue=None, per_type=None, free=8192, floor=2048, override="auto"):
    return {
        "free_ram_mb": free,
        "headroom_floor_mb": floor,
        "admission_override": override,
        "seats": seats,
        "queue": queue or [],
        "per_type": per_type
        or {
            "desktop": {"max_seats": 1, "occupying": 0, "waiting": 0},
            "terminal": {"max_seats": 2, "occupying": 0, "waiting": 0},
        },
    }


# -- planning ----------------------------------------------------------------
def test_plan_from_config_uses_max_seats():
    slots = plan_from_config(_config(desktop=2, terminal=3))
    assert [slot.key for slot in slots] == [
        "desktop-0",
        "desktop-1",
        "terminal-0",
        "terminal-1",
        "terminal-2",
    ]


def test_initial_state_is_all_off():
    wall = MonitorWall(_config(desktop=1, terminal=2))
    state = wall.initial_state()
    assert [slot.key for slot in state.slots] == ["desktop-0", "terminal-0", "terminal-1"]
    assert all(not slot.occupied for slot in state.slots)
    assert all(slot.off_text == NO_SIGNAL for slot in state.slots)
    assert state.waiters == () and state.attention == ()


def test_update_marks_occupied_and_off_slots():
    wall = MonitorWall(_config(desktop=1, terminal=2))
    status = _status(
        seats=[_seat(1, "terminal-1", agent="alice")],
        queue=[_claimed(7, 1, "alice", project="api")],
    )
    state = wall.update(status, now=NOW)
    by_key = {slot.key: slot for slot in state.slots}
    assert by_key["terminal-0"].occupied
    assert by_key["terminal-0"].agent == "alice"
    assert by_key["terminal-0"].project == "api"
    assert not by_key["terminal-1"].occupied
    assert by_key["terminal-1"].off_text == NO_SIGNAL
    assert not by_key["desktop-0"].occupied


def test_slots_are_stable_across_teardown():
    wall = MonitorWall(_config(desktop=1, terminal=2))
    first = _status(seats=[_seat(1, "terminal-1", agent="alice")])
    before = wall.update(first, now=NOW)
    assert before.slots[1].agent == "alice"

    # The seat is gone: its slot turns off in place; no slot disappears.
    after = wall.update(_status(seats=[]), now=NOW)
    assert [slot.key for slot in after.slots] == [slot.key for slot in before.slots]
    assert not after.slots[1].occupied
    assert after.slots[1].off_text == NO_SIGNAL


def test_teardown_turns_only_its_own_slot_off_and_never_moves_others():
    # terminal-1 (seat 1) lands in slot 0, terminal-2 (seat 2) in slot 1.
    wall = MonitorWall(_config(desktop=0, terminal=2))
    per_type = {"desktop": {"max_seats": 0}, "terminal": {"max_seats": 2}}
    before = wall.update(
        _status(
            seats=[_seat(1, "terminal-1", agent="alice"), _seat(2, "terminal-2", agent="bob")],
            per_type=per_type,
        ),
        now=NOW,
    )
    assert before.slots[0].agent == "alice"
    assert before.slots[1].agent == "bob"

    # terminal-1 exits: its own slot goes off, terminal-2 must not move.
    after = wall.update(
        _status(seats=[_seat(2, "terminal-2", agent="bob")], per_type=per_type), now=NOW
    )
    assert not after.slots[0].occupied  # terminal-1's slot is off...
    assert after.slots[1].agent == "bob"  # ...terminal-2 did not move.


def test_labels_elapsed_lease_and_heartbeat():
    wall = MonitorWall(_config(desktop=1, terminal=1))
    status = _status(
        seats=[
            _seat(
                1,
                "terminal-1",
                state="busy",
                agent="alice",
                lease_expires_at="2026-01-01T12:30:00.000000Z",
                last_heartbeat="2026-01-01T11:59:15.000000Z",
            )
        ],
        queue=[_claimed(7, 1, "alice", project="web")],
        per_type={"terminal": {"max_seats": 1}, "desktop": {"max_seats": 1}},
    )
    state = wall.update(status, now=NOW)
    slot = state.slots[1]
    assert slot.agent == "alice"
    assert slot.project == "web"
    assert slot.state == "busy"
    assert slot.elapsed == "30:00"
    assert slot.lease == "30m00s left"
    assert slot.heartbeat == "ok 45s"


def test_heartbeat_marks_stale():
    assert heartbeat_text("2026-01-01T11:00:00.000000Z", now=NOW, timeout_s=300) == "stale 1h00m"
    assert heartbeat_text(None, now=NOW, timeout_s=300) == "-"


# -- terminal text -----------------------------------------------------------
def test_render_terminal_text_idle_with_no_execs():
    assert render_terminal_text([]) == TERMINAL_IDLE
    assert render_terminal_text(None) == TERMINAL_IDLE


def test_render_terminal_text_prefers_running_exec():
    execs = [
        {
            "exec_id": "a",
            "label": "build",
            "state": "finished",
            "exit_code": 0,
            "started_at": "2026-01-01T11:00:00.000000Z",
            "stdout": "old output",
            "stderr": "",
        },
        {
            "exec_id": "b",
            "label": "test",
            "state": "running",
            "exit_code": None,
            "started_at": "2026-01-01T11:30:00.000000Z",
            "stdout": "$ pytest\n1 passed",
            "stderr": "",
        },
    ]
    text = render_terminal_text(execs)
    assert text.startswith("test (running)")
    assert "1 passed" in text


def test_render_terminal_text_falls_back_to_latest_and_trims_tail():
    execs = [
        {
            "exec_id": "a",
            "label": "build",
            "state": "finished",
            "exit_code": 2,
            "started_at": "2026-01-01T11:00:00.000000Z",
            "stdout": "\n".join(f"line-{i}" for i in range(50)),
            "stderr": "",
        }
    ]
    text = render_terminal_text(execs)
    assert text.startswith("build (exit 2)")
    assert text.rstrip().endswith("line-49")


def test_terminal_slot_uses_exec_text_and_desktop_slot_uses_thumbnail():
    wall = MonitorWall(_config(desktop=1, terminal=1))
    status = _status(
        seats=[
            _seat(1, "desktop-1", seat_type="desktop", agent="gfx"),
            _seat(2, "terminal-1", agent="cli"),
        ],
        queue=[_claimed(7, 2, "cli")],
        per_type={"desktop": {"max_seats": 1}, "terminal": {"max_seats": 1}},
    )
    execs = {
        2: [
            {
                "exec_id": "e",
                "label": "run",
                "state": "running",
                "started_at": "2026-01-01T11:00:00.000000Z",
                "stdout": "hello",
                "stderr": "",
            }
        ]
    }
    state = wall.update(status, execs_by_seat=execs, screenshots={1: "QUJD"}, now=NOW)
    desktop = next(slot for slot in state.slots if slot.seat_type == "desktop")
    terminal = next(slot for slot in state.slots if slot.seat_type == "terminal")
    assert desktop.thumbnail_source == "data:image/png;base64,QUJD"
    assert desktop.terminal_text == ""
    assert terminal.thumbnail_source == ""
    assert "hello" in terminal.terminal_text


def test_desktop_without_screenshot_has_no_source():
    wall = MonitorWall(_config(desktop=1, terminal=0))
    status = _status(
        seats=[_seat(1, "desktop-1", seat_type="desktop", agent="gfx")],
        per_type={"desktop": {"max_seats": 1}, "terminal": {"max_seats": 0}},
    )
    state = wall.update(status, now=NOW)
    assert state.slots[0].occupied
    assert state.slots[0].thumbnail_source == ""


# -- queue + attention -------------------------------------------------------
def test_queue_sidebar_rows_and_next_up():
    wall = MonitorWall(_config(terminal=2))
    status = _status(
        seats=[],
        queue=[
            {
                "id": 1,
                "agent_label": "bob",
                "seat_type": "terminal",
                "project": "api",
                "image": "omavroom-base",
                "status": "waiting",
                "position": 1,
                "seat_id": None,
                "queue_ahead": 0,
                "created_at": "2026-01-01T11:58:00.000000Z",
            },
            {
                "id": 2,
                "agent_label": "carol",
                "seat_type": "terminal",
                "project": None,
                "image": "omavroom-base",
                "status": "waiting",
                "position": 2,
                "seat_id": None,
                "queue_ahead": 1,
                "created_at": "2026-01-01T11:59:00.000000Z",
            },
        ],
    )
    state = wall.update(status, now=NOW)
    assert [w.agent for w in state.waiters] == ["bob", "carol"]
    assert state.waiters[0].next_up is True
    assert state.waiters[1].next_up is False
    assert state.waiters[0].project == "api"
    assert state.waiters[0].waited == "2m00s"
    assert [row["agent"] for row in state.waiter_dicts()] == ["bob", "carol"]


def test_attention_panel_lists_held_seats_with_actions():
    wall = MonitorWall(_config(terminal=1))
    status = _status(
        seats=[
            _seat(
                1,
                "terminal-1",
                state="held",
                agent="dave",
                needs_attention=True,
                last_error="export failed",
            )
        ],
        queue=[_claimed(7, 1, "dave", project="demo")],
        per_type={"desktop": {"max_seats": 0}, "terminal": {"max_seats": 1}},
    )
    state = wall.update(status, now=NOW)
    assert len(state.attention) == 1
    item = state.attention[0]
    assert item.seat_id == 1
    assert item.name == "terminal-1"
    assert item.last_error == "export failed"
    assert tuple(item.actions) == ATTENTION_ACTIONS
    assert {action["id"] for action in item.actions} == {
        "retry-release",
        "force-discard",
        "destroy",
    }


def test_pool_summary_text():
    wall = MonitorWall(_config(desktop=1, terminal=2))
    state = wall.update(
        _status(
            seats=[],
            per_type={
                "desktop": {"occupying": 1, "max_seats": 1, "waiting": 0},
                "terminal": {"occupying": 0, "max_seats": 2, "waiting": 3},
            },
        ),
        now=NOW,
    )
    assert state.pool_text == "desktop 1/1  terminal 0/2, 3 waiting"
    assert state.free_text == "8.0 GiB"
    assert state.headroom_mb == 6144


def test_update_tolerates_malformed_payload():
    wall = MonitorWall(_config(desktop=0, terminal=1))
    status = _status(seats=[], free="abc")
    status["per_type"] = [1, 2]
    state = wall.update(status, now=NOW)
    assert state.per_type == {}
    assert state.free_ram_mb is None
    assert state.free_text == "-"
    assert state.headroom_mb is None
    assert state.pool_text == "no seat types configured"


def test_plan_sync_rebuilds_only_on_settings_change():
    wall = MonitorWall(_config(desktop=0, terminal=1))
    assert wall.sync_plan({"terminal": {"max_seats": 1}}) is False
    assert [slot.key for slot in wall.plan] == ["terminal-0"]
    assert wall.sync_plan({"terminal": {"max_seats": 2}}) is True
    assert [slot.key for slot in wall.plan] == ["terminal-0", "terminal-1"]


# -- layout ------------------------------------------------------------------
def test_grid_columns_packing():
    assert grid_columns(0) == 1
    assert grid_columns(100) == 1
    assert grid_columns(260) == 1
    assert grid_columns(519) == 1
    assert grid_columns(520) == 2
    assert grid_columns(1040) == 4
    assert grid_columns(100000) == 4  # capped


def test_tile_spans():
    assert tile_column_span("desktop", 4) == 2
    assert tile_column_span("desktop", 1) == 1
    assert tile_column_span("terminal", 4) == 1
    assert tile_row_span("desktop") == 2
    assert tile_row_span("terminal") == 1


def _assert_inside(rects, width, height):
    for rect in rects:
        assert rect.x >= -0.001
        assert rect.y >= -0.001
        assert rect.x + rect.width <= width + 0.001
        assert rect.y + rect.height <= height + 0.001


def _assert_no_overlap(rects):
    for i, first in enumerate(rects):
        for second in rects[i + 1 :]:
            overlap_x = min(first.x + first.width, second.x + second.width) - max(first.x, second.x)
            overlap_y = min(first.y + first.height, second.y + second.height) - max(
                first.y, second.y
            )
            assert not (overlap_x > 0.5 and overlap_y > 0.5), (first, second)


@pytest.mark.parametrize(
    "seat_types",
    [
        ["desktop", "terminal", "terminal"],
        ["terminal", "terminal"],
        ["desktop"],
        ["desktop", "desktop", "terminal", "terminal"],
        ["terminal", "terminal", "terminal", "terminal", "terminal"],
    ],
)
@pytest.mark.parametrize("size", [(930.0, 700.0), (600.0, 900.0), (1600.0, 500.0), (400.0, 300.0)])
def test_wall_layout_always_fits_inside_the_area(seat_types, size):
    """The wall must never scroll: every slot fits, with no overlaps."""
    width, height = size
    rects = wall_layout(seat_types, width, height)
    assert len(rects) == len(seat_types)
    _assert_inside(rects, width, height)
    _assert_no_overlap(rects)


def test_wall_layout_fills_the_available_space():
    rects = wall_layout(["desktop", "terminal", "terminal"], 930.0, 700.0)
    # No empty band below or beside the tiles.
    assert min(r.y for r in rects) <= 0.001
    assert min(r.x for r in rects) <= 0.001
    assert max(r.y + r.height for r in rects) >= 699.0
    assert max(r.x + r.width for r in rects) >= 929.0


def test_focus_makes_the_selected_tile_largest_and_keeps_the_rest_visible():
    width, height = 930.0, 700.0
    seat_types = ["desktop", "terminal", "terminal"]
    rects = wall_layout(seat_types, width, height, focus_index=1)
    assert rects[1].focused is True
    assert all(not rect.focused for index, rect in enumerate(rects) if index != 1)
    # Focused tile spans the full width across the TOP so it stays landscape.
    assert rects[1].x == 0.0 and rects[1].y == 0.0
    assert rects[1].width == width
    assert rects[1].width > rects[1].height  # landscape, not a portrait panel
    focused_area = rects[1].width * rects[1].height
    for index, rect in enumerate(rects):
        if index != 1:
            assert focused_area > rect.width * rect.height
            # The others sit in the strip BELOW the focused monitor.
            assert rect.y >= rects[1].height
            # ...and are still on the wall (not collapsed to nothing).
            assert rect.width > 1.0 and rect.height > 1.0
    _assert_inside(rects, width, height)
    _assert_no_overlap(rects)


def test_missing_screenshot_keeps_the_previous_frame():
    """A poll without a fresh screenshot must not blank the monitor (the
    'flash of black' seen every few seconds)."""
    wall = MonitorWall(_config(desktop=1, terminal=2))
    wall.sync_plan({"desktop": {"max_seats": 1}, "terminal": {"max_seats": 2}})
    status = {
        "seats": [_seat(1, "desktop-1", seat_type="desktop", state="ready")],
        "queue": [],
        "per_type": {"desktop": {"max_seats": 1}, "terminal": {"max_seats": 2}},
    }
    first = wall.update(status, screenshots={1: "AAAA"})
    assert "AAAA" in first.slots[0].thumbnail_source
    # No screenshot this poll -> the previous frame is retained, not cleared.
    second = wall.update(status, screenshots={})
    assert "AAAA" in second.slots[0].thumbnail_source
    # A fresh frame replaces it.
    third = wall.update(status, screenshots={1: "BBBB"})
    assert "BBBB" in third.slots[0].thumbnail_source


def test_single_slot_focus_is_a_no_op():
    rects = wall_layout(["terminal"], 800.0, 600.0, focus_index=0)
    assert len(rects) == 1
    assert rects[0].width == 800.0 and rects[0].height == 600.0


def test_choose_columns_is_a_valid_candidate():
    seat_types = ["desktop", "terminal", "terminal"]
    columns = choose_columns(seat_types, 930.0, 700.0)
    assert 1 <= columns <= len(seat_types)
    # A wide wall should not fall back to a single tall column.
    assert choose_columns(["terminal", "terminal"], 1600.0, 500.0) >= 2
