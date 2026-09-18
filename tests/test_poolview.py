"""Pure read-model tests for the Phase 6 CLI/TUI shared view (no daemon)."""

from __future__ import annotations

from datetime import UTC, datetime

from omavroom.poolview import (
    NO_SIGNAL,
    SeatRow,
    Slot,
    assign_slots,
    build_seat_rows,
    build_waiter_rows,
    format_elapsed,
    format_lease,
    format_mb,
    format_short,
    headroom_mb,
    next_up_ids,
    parse_time,
    pool_totals,
    render_slot,
    slot_plan,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _status(*, seats, queue=None, per_type=None, free=8192, floor=2048, override="auto"):
    return {
        "free_ram_mb": free,
        "headroom_floor_mb": floor,
        "admission_override": override,
        "seats": seats,
        "queue": queue or [],
        "per_type": per_type or {},
        "needs_attention": [s["id"] for s in seats if s.get("needs_attention")],
    }


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


# -- formatting --------------------------------------------------------------
def test_format_helpers():
    assert parse_time("2026-01-01T00:00:00.000000Z") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_time(None) is None and parse_time("garbage") is None
    assert format_elapsed(None) == "-"
    assert format_elapsed(0) == "00:00"
    assert format_elapsed(75) == "01:15"
    assert format_elapsed(3725) == "1:02:05"
    assert format_short(45) == "45s"
    assert format_short(754) == "12m34s"
    assert format_short(4320) == "1h12m"
    assert format_mb(512) == "512 MiB"
    assert format_mb(8192) == "8.0 GiB"


def test_lease_formatting():
    assert format_lease(None, NOW) == "-"
    assert format_lease("2026-01-01T11:59:00.000000Z", NOW) == "expired"
    assert format_lease("2026-01-01T12:30:00.000000Z", NOW) == "30m00s left"


# -- seat rows ---------------------------------------------------------------
def test_build_seat_rows_joins_claimed_request():
    status = _status(
        seats=[
            _seat(
                1,
                "terminal-1",
                state="busy",
                agent="alice",
                lease_expires_at="2026-01-01T13:00:00.000000Z",
            )
        ],
        queue=[_claimed(7, 1, "alice", project="web")],
    )
    rows = build_seat_rows(status, now=NOW)
    assert len(rows) == 1
    row = rows[0]
    assert row.agent == "alice"
    assert row.project == "web"
    assert row.state == "busy"
    assert row.elapsed_s == 1800.0
    assert format_lease(row.lease_expires_at, NOW) == "1h00m left"


def test_build_seat_rows_sorts_by_type_then_id():
    status = _status(
        seats=[
            _seat(3, "terminal-2", state="ready"),
            _seat(1, "desktop-1", seat_type="desktop", state="ready"),
            _seat(2, "terminal-1", state="ready"),
        ],
    )
    rows = build_seat_rows(status, now=NOW)
    assert [row.name for row in rows] == ["desktop-1", "terminal-1", "terminal-2"]


# -- waiters -----------------------------------------------------------------
def test_build_waiter_rows_and_next_up():
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
    waiters = build_waiter_rows(status, now=NOW)
    assert [w.agent for w in waiters] == ["bob", "carol"]
    assert next_up_ids(waiters) == {1}
    assert waiters[0].waited_s == 120.0


# -- slots: the fixed-set stability contract ---------------------------------
def test_slot_plan_derived_from_settings_only():
    per_type = {
        "desktop": {"max_seats": 2},
        "terminal": {"max_seats": 1},
    }
    slots = slot_plan(per_type)
    assert [slot.key for slot in slots] == ["desktop-0", "desktop-1", "terminal-0"]
    assert slots[0].name == "desktop-1"


def test_slot_plan_ignores_live_seat_counts():
    # A busy pool and an empty pool with the same settings plan the same slots.
    busy = slot_plan({"desktop": {"max_seats": 1}, "terminal": {"max_seats": 2}})
    empty = slot_plan({"desktop": {"max_seats": 1}, "terminal": {"max_seats": 2}})
    assert [s.key for s in busy] == [s.key for s in empty]


def test_assign_slots_is_stable_on_teardown():
    slots = slot_plan({"desktop": {"max_seats": 1}, "terminal": {"max_seats": 2}})
    first, second = Slot("terminal", 0), Slot("terminal", 1)
    rows = [
        SeatRow(
            seat_id=1,
            name="terminal-1",
            seat_type="terminal",
            state="ready",
            agent="alice",
            project="web",
            image="omavroom-base",
            vm_name="fake://terminal-1",
            elapsed_s=10,
            lease_expires_at=None,
            last_heartbeat=None,
            needs_attention=False,
            last_error=None,
        ),
        SeatRow(
            seat_id=2,
            name="terminal-2",
            seat_type="terminal",
            state="ready",
            agent="bob",
            project=None,
            image="omavroom-base",
            vm_name="fake://terminal-2",
            elapsed_s=20,
            lease_expires_at=None,
            last_heartbeat=None,
            needs_attention=False,
            last_error=None,
        ),
    ]
    assignment = assign_slots(slots, rows)
    assert assignment[first.key].agent == "alice"
    assert assignment[second.key].agent == "bob"
    previous = {key: (row.seat_id if row else None) for key, row in assignment.items()}

    # Teardown of terminal-1 (the first slot) leaves terminal-2 in its slot
    # and turns only terminal-1 off, with no reflow.
    after = assign_slots(slots, [rows[1]], previous=previous)
    assert after[first.key] is None
    assert after[second.key].agent == "bob"

    # A brand-new seat fills the freed lowest slot without disturbing the other.
    newcomer = SeatRow(
        seat_id=9,
        name="terminal-3",
        seat_type="terminal",
        state="ready",
        agent="carol",
        project=None,
        image="omavroom-base",
        vm_name="fake://terminal-3",
        elapsed_s=0,
        lease_expires_at=None,
        last_heartbeat=None,
        needs_attention=False,
        last_error=None,
    )
    refilled = assign_slots(slots, [rows[1], newcomer], previous=previous)
    assert refilled[first.key].agent == "carol"
    assert refilled[second.key].agent == "bob"


def test_render_slot_off_and_live():
    off = render_slot(Slot("terminal", 1), None)
    assert "terminal-2" in off and NO_SIGNAL in off
    row = SeatRow(
        seat_id=1,
        name="terminal-1",
        seat_type="terminal",
        state="busy",
        agent="alice",
        project="web",
        image="omavroom-base",
        vm_name="fake://terminal-1",
        elapsed_s=65,
        lease_expires_at="2026-01-01T12:30:00.000000Z",
        last_heartbeat=None,
        needs_attention=False,
        last_error=None,
    )
    live = render_slot(Slot("terminal", 0), row, now=NOW)
    assert "agent    alice" in live
    assert "project  web" in live
    assert "state    busy" in live
    assert "elapsed  01:05" in live
    assert "30m00s left" in live


def test_pool_totals_and_headroom():
    per_type = {
        "desktop": {"occupying": 1, "max_seats": 1, "waiting": 0},
        "terminal": {"occupying": 0, "max_seats": 2, "waiting": 3},
    }
    assert pool_totals(per_type) == "desktop 1/1  terminal 0/2, 3 waiting"
    assert headroom_mb(_status(seats=[], free=8192, floor=2048)) == 6144
