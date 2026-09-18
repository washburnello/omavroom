"""Phase 4 state layer: enums, transactions, queue order, leases, restart."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from omavroom import state as st


def _t(offset_s: int = 0) -> datetime:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return base + timedelta(seconds=offset_s)


def _store(tmp_path) -> st.StateStore:
    store = st.StateStore(tmp_path / "state.db")
    store.init()
    return store


def _insert_seat(store, *, state="off", seat_type="terminal", name="terminal-1") -> int:
    with store.transaction() as conn:
        return st.insert_seat(
            conn,
            name=name,
            seat_type=seat_type,
            image="omavroom-base",
            state=state,
            now=st.fmt_time(_t()),
        )


def test_seat_states_cover_the_full_enum() -> None:
    assert {s.value for s in st.SeatState} == {
        "off",
        "queued",
        "provisioning",
        "ready",
        "busy",
        "resetting",
        "releasing",
        "held",
        "error",
    }


def test_schema_rejects_invalid_seat_state(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_seat(store, state="banana")


def test_schema_rejects_invalid_seat_type(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_seat(store, seat_type="laptop")


def test_schema_rejects_invalid_request_status(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO queue (agent_label, seat_type, status, position,"
                " created_at, updated_at) VALUES ('a', 'terminal', 'banana', 1, ?, ?)",
                (st.fmt_time(_t()), st.fmt_time(_t())),
            )


def test_queue_positions_are_stable_and_fifo(tmp_path) -> None:
    store = _store(tmp_path)
    with store.transaction() as conn:
        a = st.enqueue_request(
            conn,
            agent_label="A",
            seat_type="terminal",
            image=None,
            project=None,
            now=st.fmt_time(_t()),
        )
        b = st.enqueue_request(
            conn,
            agent_label="B",
            seat_type="terminal",
            image=None,
            project=None,
            now=st.fmt_time(_t(1)),
        )
        c = st.enqueue_request(
            conn,
            agent_label="C",
            seat_type="desktop",
            image=None,
            project=None,
            now=st.fmt_time(_t(2)),
        )
    with store.connect() as conn:
        assert st.request_by_id(conn, a).position == 1
        assert st.request_by_id(conn, b).position == 2
        assert st.request_by_id(conn, c).position == 1
        head = st.next_waiting(conn, "terminal")
        assert head is not None and head.agent_label == "A"
        assert st.count_waiting_ahead(conn, head) == 0


def test_transaction_rolls_back_on_error(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            st.insert_seat(
                conn,
                name="terminal-1",
                seat_type="terminal",
                image="img",
                now=st.fmt_time(_t()),
            )
            raise RuntimeError("boom")
    assert store.read(st.list_seats) == []


def test_state_survives_process_restart(tmp_path) -> None:
    path = tmp_path / "state.db"
    first = st.StateStore(path)
    first.init()
    seat_id = _insert_seat(first, state="ready", name="terminal-7")
    with first.transaction() as conn:
        request_id = st.enqueue_request(
            conn,
            agent_label="agent",
            seat_type="terminal",
            image=None,
            project="proj",
            now=st.fmt_time(_t()),
        )
        st.update_request(
            conn, request_id, status="claimed", seat_id=seat_id, now=st.fmt_time(_t(1))
        )
        st.create_lease(
            conn,
            seat_id=seat_id,
            request_id=request_id,
            agent_label="agent",
            now=st.fmt_time(_t(1)),
            lease_timeout_s=1800,
        )

    second = st.StateStore(path)
    second.init()
    seats = second.read(st.list_seats)
    assert [s.name for s in seats] == ["terminal-7"]
    assert seats[0].state == "ready"
    request = second.read(lambda c: st.request_by_id(c, request_id))
    assert request is not None and request.status == "claimed" and request.seat_id == seat_id
    lease = second.read(lambda c: st.active_lease_for_seat(c, seat_id))
    assert lease is not None and lease.agent_label == "agent"


def test_lease_wall_clock_cap_and_heartbeat_timeout(tmp_path) -> None:
    store = _store(tmp_path)
    seat_id = _insert_seat(store, state="ready")
    now = _t()
    with store.transaction() as conn:
        lease_id = st.create_lease(
            conn,
            seat_id=seat_id,
            request_id=None,
            agent_label="a",
            now=st.fmt_time(now),
            lease_timeout_s=1800,
        )
    with store.transaction() as conn:
        st.touch_heartbeat(conn, lease_id, st.fmt_time(_t(1799)))
    # Heartbeat is fresh at +1799 and the wall-clock cap is not yet reached.
    assert store.read(lambda c: st.expired_leases(c, _t(1799), 300)) == []
    # Wall clock expires at +1800 even though the heartbeat is fresh.
    expired = store.read(lambda c: st.expired_leases(c, _t(1801), 300))
    assert [lease.id for lease in expired] == [lease_id]

    # A separate lease with no heartbeat dies at the heartbeat timeout.
    seat2 = _insert_seat(store, state="ready", name="terminal-2")
    with store.transaction() as conn:
        lease2 = st.create_lease(
            conn,
            seat_id=seat2,
            request_id=None,
            agent_label="b",
            now=st.fmt_time(now),
            lease_timeout_s=10_000,
        )
    assert [lease.id for lease in store.read(lambda c: st.expired_leases(c, _t(301), 300))] == [
        lease2
    ]


def test_only_one_active_lease_per_seat(tmp_path) -> None:
    store = _store(tmp_path)
    seat_id = _insert_seat(store, state="ready")
    with store.transaction() as conn:
        st.create_lease(
            conn,
            seat_id=seat_id,
            request_id=None,
            agent_label="a",
            now=st.fmt_time(_t()),
            lease_timeout_s=1800,
        )
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as conn:
            st.create_lease(
                conn,
                seat_id=seat_id,
                request_id=None,
                agent_label="b",
                now=st.fmt_time(_t()),
                lease_timeout_s=1800,
            )


def test_events_are_audited(tmp_path) -> None:
    store = _store(tmp_path)
    with store.transaction() as conn:
        st.log_event(
            conn,
            event_type="seat_ready",
            now=st.fmt_time(_t()),
            seat_id=1,
            agent_label="agent",
            detail="vm=fake://terminal-1",
        )
    events = store.read(st.list_events)
    assert len(events) == 1
    assert events[0].event_type == "seat_ready"
    assert events[0].detail == "vm=fake://terminal-1"


def test_canonical_time_round_trip() -> None:
    moment = _t(12345)
    assert st.parse_time(st.fmt_time(moment)) == moment
    assert st.plus_seconds(moment, 60) == moment + timedelta(seconds=60)


def test_schema_migrates_v1_to_v2_without_data_loss(tmp_path) -> None:
    path = tmp_path / "v1.db"
    conn = st.connect(path)
    conn.executescript(
        "CREATE TABLE seats ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL UNIQUE,"
        " seat_type TEXT NOT NULL,"
        " state TEXT NOT NULL DEFAULT 'off',"
        " vm_name TEXT,"
        " image TEXT NOT NULL,"
        " agent_label TEXT,"
        " last_error TEXT,"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL);"
        "PRAGMA user_version = 1;"
    )
    conn.execute(
        "INSERT INTO seats (name, seat_type, state, image, created_at, updated_at)"
        " VALUES ('terminal-1', 'terminal', 'ready', 'img', ?, ?)",
        (st.fmt_time(_t()), st.fmt_time(_t())),
    )
    conn.commit()

    st.init_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(seats);")}
    assert "attempts" in columns
    version = conn.execute("PRAGMA user_version;").fetchone()[0]
    assert version == st.SCHEMA_VERSION
    seat = st.list_seats(conn)[0]
    assert seat.name == "terminal-1" and seat.state == "ready" and seat.attempts == 0


def test_schema_migrates_v2_to_v3_adds_pending_intent(tmp_path) -> None:
    """v2 -> v3 adds the interrupted-operation intent columns in place."""
    path = tmp_path / "v2.db"
    conn = st.connect(path)
    conn.executescript(
        "CREATE TABLE seats ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL UNIQUE,"
        " seat_type TEXT NOT NULL,"
        " state TEXT NOT NULL DEFAULT 'off',"
        " vm_name TEXT,"
        " image TEXT NOT NULL,"
        " agent_label TEXT,"
        " last_error TEXT,"
        " attempts INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL);"
        "PRAGMA user_version = 2;"
    )
    conn.execute(
        "INSERT INTO seats (name, seat_type, state, image, attempts, created_at, updated_at)"
        " VALUES ('terminal-1', 'terminal', 'ready', 'img', 2, ?, ?)",
        (st.fmt_time(_t()), st.fmt_time(_t())),
    )
    conn.commit()

    st.init_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(seats);")}
    assert {
        "pending_action",
        "pending_export",
        "pending_repo",
        "pending_branch",
        "pending_ref",
        "pending_request_status",
    } <= columns
    assert conn.execute("PRAGMA user_version;").fetchone()[0] == st.SCHEMA_VERSION
    seat = st.list_seats(conn)[0]
    assert seat.attempts == 2 and seat.pending_action is None

    # The persisted intent round-trips (used by reconcile resume).
    with st.StateStore(path).transaction() as tx:
        st.update_seat(
            tx,
            seat.id,
            pending_action="release",
            pending_export=1,
            pending_repo="demo",
            pending_branch="task",
            now=st.fmt_time(_t(1)),
        )
    reloaded = st.StateStore(path).read(lambda c: st.seat_by_id(c, seat.id))
    assert reloaded.pending_action == "release"
    assert reloaded.pending_repo == "demo"
    assert reloaded.pending_branch == "task"
    conn.close()
