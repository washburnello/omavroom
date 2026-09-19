"""SQLite state layer for the omavroom scheduler (Phase 4).

This module owns the durable state machine: seats, the request queue,
leases, and an events/audit log. The scheduler keeps *all* authoritative
state here (never only in memory) so a daemon restart reconnects to the
same sqlite file and reattaches to whatever was running.

Design decisions
----------------
- **CHECK-constrained enums.** Seat states and request statuses are
  enforced by SQLite CHECK constraints so an invalid transition can never
  be persisted, even by a buggy caller. ``SeatState``/``RequestStatus``
  are the single source of truth and the schema is generated from them.
- **Canonical UTC timestamps.** Every timestamp is written by Python as
  ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (fixed width), so lexicographic string
  comparison is also chronological comparison in SQL.
- **Transactions.** :meth:`StateStore.transaction` opens a fresh
  connection, runs ``BEGIN IMMEDIATE`` (write lock acquired up front, so
  check-then-claim can never interleave), and commits or rolls back. One
  connection per transaction keeps the store safe to share across threads
  and across short-lived readers.
- **Lease semantics.** ``expires_at`` is the wall-clock hard cap set at
  acquisition and *never* renewed; ``last_heartbeat`` is renewed by the
  independent heartbeat channel. A lease is dead if either the wall clock
  passed ``expires_at`` or ``last_heartbeat`` is older than the configured
  heartbeat timeout. This keeps a chatty agent from holding a seat forever.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 4

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class SeatState(StrEnum):
    """Lifecycle states for a seat (see scheduler transition table)."""

    OFF = "off"
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    READY = "ready"
    BUSY = "busy"
    RESETTING = "resetting"
    RELEASING = "releasing"
    HELD = "held"
    ERROR = "error"


class RequestStatus(StrEnum):
    """Lifecycle states for a queued seat request."""

    WAITING = "waiting"
    CLAIMED = "claimed"
    DONE = "done"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


#: Seat states that occupy a resource slot and count against ``max_seats``.
#: ``held`` counts (a preserved VM still uses RAM); ``error``/``off`` do not.
OCCUPYING_SEAT_STATES: tuple[str, ...] = (
    SeatState.QUEUED.value,
    SeatState.PROVISIONING.value,
    SeatState.READY.value,
    SeatState.BUSY.value,
    SeatState.RESETTING.value,
    SeatState.RELEASING.value,
    SeatState.HELD.value,
)

#: Request statuses that are final; such requests are never acted on again.
TERMINAL_REQUEST_STATES: tuple[str, ...] = (
    RequestStatus.DONE.value,
    RequestStatus.CANCELLED.value,
    RequestStatus.EXPIRED.value,
    RequestStatus.FAILED.value,
)


def _check_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS seats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    seat_type TEXT NOT NULL CHECK (seat_type IN ('desktop', 'terminal')),
    state TEXT NOT NULL DEFAULT 'off'
        CHECK (state IN ({_check_in(tuple(s.value for s in SeatState))})),
    vm_name TEXT,
    image TEXT NOT NULL,
    agent_label TEXT,
    last_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    pending_action TEXT CHECK (pending_action IN ('release', 'reset')),
    pending_export INTEGER,
    pending_repo TEXT,
    pending_branch TEXT,
    pending_ref TEXT,
    pending_request_status TEXT,
    export_repo TEXT,
    export_branch TEXT,
    export_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seat_id INTEGER NOT NULL REFERENCES seats (id) ON DELETE CASCADE,
    request_id INTEGER REFERENCES queue (id) ON DELETE SET NULL,
    agent_label TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL,
    released_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_active_seat
    ON leases (seat_id) WHERE released_at IS NULL;
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_label TEXT NOT NULL,
    seat_type TEXT NOT NULL CHECK (seat_type IN ('desktop', 'terminal')),
    project TEXT,
    image TEXT,
    status TEXT NOT NULL DEFAULT 'waiting'
        CHECK (status IN ({_check_in(tuple(s.value for s in RequestStatus))})),
    position INTEGER NOT NULL,
    seat_id INTEGER REFERENCES seats (id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_waiting
    ON queue (seat_type, position) WHERE status = 'waiting';
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    seat_id INTEGER,
    request_id INTEGER,
    agent_label TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events (at);
"""

#: ``seats`` columns added by the v3 migration, in dependency-free order.
#: SQLite cannot add a CHECK constraint with ``ALTER TABLE ADD COLUMN``, so a
#: v2 DB migrated in place has no ``pending_action`` CHECK while a fresh v3
#: schema does. The value is written only by the scheduler
#: (``release``/``reset``), so this asymmetry is intentional and documented
#: rather than papered over with a table rebuild.
_SEAT_PENDING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("pending_action", "TEXT"),
    ("pending_export", "INTEGER"),
    ("pending_repo", "TEXT"),
    ("pending_branch", "TEXT"),
    ("pending_ref", "TEXT"),
    ("pending_request_status", "TEXT"),
)

#: ``seats`` columns added by the v4 migration: the durable export intent
#: (repo/branch/ref) recorded by ``prepare_repo``/``export_seat``. Stasis
#: (heartbeat/lease reclaim) re-runs the normal gated export against this
#: intent instead of destroying the VM.
_SEAT_EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("export_repo", "TEXT"),
    ("export_branch", "TEXT"),
    ("export_ref", "TEXT"),
)


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def fmt_time(moment: datetime) -> str:
    """Format a datetime as the canonical fixed-width UTC string."""
    return moment.astimezone(UTC).strftime(_TIME_FORMAT)


def parse_time(value: str) -> datetime:
    """Parse a canonical stored timestamp back into a UTC datetime."""
    return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=UTC)


def plus_seconds(moment: datetime, seconds: int) -> datetime:
    """Return ``moment + seconds`` in UTC."""
    return moment.astimezone(UTC) + timedelta(seconds=seconds)


@dataclass(frozen=True)
class Seat:
    id: int
    name: str
    seat_type: str
    state: str
    vm_name: str | None
    image: str
    agent_label: str | None
    last_error: str | None
    attempts: int
    pending_action: str | None
    pending_export: int | None
    pending_repo: str | None
    pending_branch: str | None
    pending_ref: str | None
    pending_request_status: str | None
    export_repo: str | None
    export_branch: str | None
    export_ref: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Request:
    id: int
    agent_label: str
    seat_type: str
    project: str | None
    image: str | None
    status: str
    position: int
    seat_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Lease:
    id: int
    seat_id: int
    request_id: int | None
    agent_label: str
    acquired_at: str
    expires_at: str
    last_heartbeat: str
    released_at: str | None


@dataclass(frozen=True)
class Event:
    id: int
    at: str
    event_type: str
    seat_id: int | None
    request_id: int | None
    agent_label: str | None
    detail: str | None


_UNSET = object()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the state database with the project's standard pragmas.

    WAL mode lets monitor/TUI readers query while the manager daemon
    writes; ``foreign_keys`` keeps leases/queue rows tied to seats.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema, preserving state across restarts.

    Phase 0 shipped a stub schema with no enum constraints and no
    events/audit table. That skeleton was never deployed, so a database
    still at ``user_version = 0`` that already has a ``seats`` table is
    dropped and recreated. Later versions are migrated in place: v1 added
    ``seats.attempts`` (bounded prewarm retries), v3 added the
    ``pending_*`` release/reset intent columns (interrupted-operation
    recovery), and v4 added the durable ``export_*`` intent columns that
    stasis re-runs instead of destroying the VM.
    """
    version = conn.execute("PRAGMA user_version;").fetchone()[0]
    existing = set(list_tables(conn))
    if version < SCHEMA_VERSION and "seats" in existing and version < 1:
        conn.executescript(
            "DROP TABLE IF EXISTS leases;"
            "DROP TABLE IF EXISTS queue;"
            "DROP TABLE IF EXISTS events;"
            "DROP TABLE IF EXISTS seats;"
        )
        version = 0
    conn.executescript(SCHEMA)
    if "seats" in existing and version < 2:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(seats);").fetchall()}
        if "attempts" not in columns:
            conn.execute("ALTER TABLE seats ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;")
    if "seats" in existing and version < 3:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(seats);").fetchall()}
        for name, ddl in _SEAT_PENDING_COLUMNS:
            if name not in columns:
                conn.execute(f"ALTER TABLE seats ADD COLUMN {name} {ddl};")
    if "seats" in existing and version < 4:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(seats);").fetchall()}
        for name, ddl in _SEAT_EXPORT_COLUMNS:
            if name not in columns:
                conn.execute(f"ALTER TABLE seats ADD COLUMN {name} {ddl};")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return the user-table names present in the connected database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


# --------------------------------------------------------------------------
# Row mapping
# --------------------------------------------------------------------------
def _seat(row: sqlite3.Row) -> Seat:
    return Seat(
        id=row["id"],
        name=row["name"],
        seat_type=row["seat_type"],
        state=row["state"],
        vm_name=row["vm_name"],
        image=row["image"],
        agent_label=row["agent_label"],
        last_error=row["last_error"],
        attempts=row["attempts"],
        pending_action=row["pending_action"],
        pending_export=row["pending_export"],
        pending_repo=row["pending_repo"],
        pending_branch=row["pending_branch"],
        pending_ref=row["pending_ref"],
        pending_request_status=row["pending_request_status"],
        export_repo=row["export_repo"],
        export_branch=row["export_branch"],
        export_ref=row["export_ref"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _request(row: sqlite3.Row) -> Request:
    return Request(
        id=row["id"],
        agent_label=row["agent_label"],
        seat_type=row["seat_type"],
        project=row["project"],
        image=row["image"],
        status=row["status"],
        position=row["position"],
        seat_id=row["seat_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _lease(row: sqlite3.Row) -> Lease:
    return Lease(
        id=row["id"],
        seat_id=row["seat_id"],
        request_id=row["request_id"],
        agent_label=row["agent_label"],
        acquired_at=row["acquired_at"],
        expires_at=row["expires_at"],
        last_heartbeat=row["last_heartbeat"],
        released_at=row["released_at"],
    )


def _event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        at=row["at"],
        event_type=row["event_type"],
        seat_id=row["seat_id"],
        request_id=row["request_id"],
        agent_label=row["agent_label"],
        detail=row["detail"],
    )


# --------------------------------------------------------------------------
# Seat queries
# --------------------------------------------------------------------------
def insert_seat(
    conn: sqlite3.Connection,
    *,
    name: str,
    seat_type: str,
    image: str,
    state: str = SeatState.OFF.value,
    vm_name: str | None = None,
    agent_label: str | None = None,
    attempts: int = 0,
    now: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO seats (name, seat_type, state, vm_name, image, agent_label,"
        " attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, seat_type, state, vm_name, image, agent_label, attempts, now, now),
    )
    return int(cur.lastrowid)


def update_seat(
    conn: sqlite3.Connection,
    seat_id: int,
    *,
    state: object = _UNSET,
    vm_name: object = _UNSET,
    agent_label: object = _UNSET,
    last_error: object = _UNSET,
    attempts: object = _UNSET,
    pending_action: object = _UNSET,
    pending_export: object = _UNSET,
    pending_repo: object = _UNSET,
    pending_branch: object = _UNSET,
    pending_ref: object = _UNSET,
    pending_request_status: object = _UNSET,
    export_repo: object = _UNSET,
    export_branch: object = _UNSET,
    export_ref: object = _UNSET,
    now: str,
) -> None:
    sets = ["updated_at = ?"]
    params: list[object] = [now]
    for column, value in (
        ("state", state),
        ("vm_name", vm_name),
        ("agent_label", agent_label),
        ("last_error", last_error),
        ("attempts", attempts),
        ("pending_action", pending_action),
        ("pending_export", pending_export),
        ("pending_repo", pending_repo),
        ("pending_branch", pending_branch),
        ("pending_ref", pending_ref),
        ("pending_request_status", pending_request_status),
        ("export_repo", export_repo),
        ("export_branch", export_branch),
        ("export_ref", export_ref),
    ):
        if value is not _UNSET:
            sets.append(f"{column} = ?")
            params.append(value)
    params.append(seat_id)
    conn.execute(f"UPDATE seats SET {', '.join(sets)} WHERE id = ?", params)


def seat_by_id(conn: sqlite3.Connection, seat_id: int) -> Seat | None:
    row = conn.execute("SELECT * FROM seats WHERE id = ?", (seat_id,)).fetchone()
    return _seat(row) if row else None


def list_seats(conn: sqlite3.Connection, seat_type: str | None = None) -> list[Seat]:
    if seat_type is None:
        rows = conn.execute("SELECT * FROM seats ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM seats WHERE seat_type = ? ORDER BY id", (seat_type,)
        ).fetchall()
    return [_seat(row) for row in rows]


def count_occupying(conn: sqlite3.Connection, seat_type: str) -> int:
    placeholders = ", ".join("?" for _ in OCCUPYING_SEAT_STATES)
    row = conn.execute(
        f"SELECT COUNT(*) FROM seats WHERE seat_type = ? AND state IN ({placeholders})",
        (seat_type, *OCCUPYING_SEAT_STATES),
    ).fetchone()
    return int(row[0])


def count_seats_in_state(conn: sqlite3.Connection, seat_type: str, states: tuple[str, ...]) -> int:
    if not states:
        return 0
    placeholders = ", ".join("?" for _ in states)
    row = conn.execute(
        f"SELECT COUNT(*) FROM seats WHERE seat_type = ? AND state IN ({placeholders})",
        (seat_type, *states),
    ).fetchone()
    return int(row[0])


# --------------------------------------------------------------------------
# Queue / request queries
# --------------------------------------------------------------------------
def enqueue_request(
    conn: sqlite3.Connection,
    *,
    agent_label: str,
    seat_type: str,
    image: str | None,
    project: str | None,
    now: str,
) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM queue WHERE seat_type = ?",
        (seat_type,),
    ).fetchone()
    position = int(row[0])
    cur = conn.execute(
        "INSERT INTO queue (agent_label, seat_type, project, image, status, position,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 'waiting', ?, ?, ?)",
        (agent_label, seat_type, project, image, position, now, now),
    )
    return int(cur.lastrowid)


def next_waiting(conn: sqlite3.Connection, seat_type: str) -> Request | None:
    """Return the head of the FIFO queue for a seat type without claiming it."""
    row = conn.execute(
        "SELECT * FROM queue WHERE seat_type = ? AND status = 'waiting'"
        " ORDER BY position, id LIMIT 1",
        (seat_type,),
    ).fetchone()
    return _request(row) if row else None


def count_waiting(conn: sqlite3.Connection, seat_type: str | None = None) -> int:
    if seat_type is None:
        row = conn.execute("SELECT COUNT(*) FROM queue WHERE status = 'waiting'").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM queue WHERE status = 'waiting' AND seat_type = ?",
            (seat_type,),
        ).fetchone()
    return int(row[0])


def count_waiting_ahead(conn: sqlite3.Connection, request: Request) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM queue WHERE status = 'waiting' AND seat_type = ?"
        " AND (position < ? OR (position = ? AND id < ?))",
        (request.seat_type, request.position, request.position, request.id),
    ).fetchone()
    return int(row[0])


def request_by_id(conn: sqlite3.Connection, request_id: int) -> Request | None:
    row = conn.execute("SELECT * FROM queue WHERE id = ?", (request_id,)).fetchone()
    return _request(row) if row else None


def request_for_seat(conn: sqlite3.Connection, seat_id: int) -> Request | None:
    """Most recent request that referenced a seat (any status), if any."""
    row = conn.execute(
        "SELECT * FROM queue WHERE seat_id = ? ORDER BY id DESC LIMIT 1", (seat_id,)
    ).fetchone()
    return _request(row) if row else None


def list_requests(
    conn: sqlite3.Connection,
    *,
    statuses: tuple[str, ...] | None = None,
    seat_type: str | None = None,
) -> list[Request]:
    clauses: list[str] = []
    params: list[object] = []
    if statuses:
        clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
        params.extend(statuses)
    if seat_type is not None:
        clauses.append("seat_type = ?")
        params.append(seat_type)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM queue{where} ORDER BY position, id", params).fetchall()
    return [_request(row) for row in rows]


def update_request(
    conn: sqlite3.Connection,
    request_id: int,
    *,
    status: object = _UNSET,
    seat_id: object = _UNSET,
    now: str,
) -> None:
    sets = ["updated_at = ?"]
    params: list[object] = [now]
    for column, value in (("status", status), ("seat_id", seat_id)):
        if value is not _UNSET:
            sets.append(f"{column} = ?")
            params.append(value)
    params.append(request_id)
    conn.execute(f"UPDATE queue SET {', '.join(sets)} WHERE id = ?", params)


# --------------------------------------------------------------------------
# Lease queries
# --------------------------------------------------------------------------
def create_lease(
    conn: sqlite3.Connection,
    *,
    seat_id: int,
    request_id: int | None,
    agent_label: str,
    now: str,
    lease_timeout_s: int,
) -> int:
    wall_clock = plus_seconds(parse_time(now), lease_timeout_s)
    cur = conn.execute(
        "INSERT INTO leases (seat_id, request_id, agent_label, acquired_at,"
        " expires_at, last_heartbeat) VALUES (?, ?, ?, ?, ?, ?)",
        (seat_id, request_id, agent_label, now, fmt_time(wall_clock), now),
    )
    return int(cur.lastrowid)


def lease_by_id(conn: sqlite3.Connection, lease_id: int) -> Lease | None:
    row = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
    return _lease(row) if row else None


def active_lease_for_seat(conn: sqlite3.Connection, seat_id: int) -> Lease | None:
    row = conn.execute(
        "SELECT * FROM leases WHERE seat_id = ? AND released_at IS NULL ORDER BY id DESC LIMIT 1",
        (seat_id,),
    ).fetchone()
    return _lease(row) if row else None


def lease_for_request(conn: sqlite3.Connection, request_id: int) -> Lease | None:
    row = conn.execute(
        "SELECT * FROM leases WHERE request_id = ? AND released_at IS NULL"
        " ORDER BY id DESC LIMIT 1",
        (request_id,),
    ).fetchone()
    return _lease(row) if row else None


def touch_heartbeat(conn: sqlite3.Connection, lease_id: int, now: str) -> None:
    conn.execute("UPDATE leases SET last_heartbeat = ? WHERE id = ?", (now, lease_id))


def release_lease(conn: sqlite3.Connection, lease_id: int, now: str) -> None:
    conn.execute("UPDATE leases SET released_at = ? WHERE id = ?", (now, lease_id))


def expired_leases(
    conn: sqlite3.Connection, now: datetime, heartbeat_timeout_s: int
) -> list[Lease]:
    """Active leases past either the wall-clock cap or the heartbeat window."""
    now_text = fmt_time(now)
    heartbeat_floor = fmt_time(now - timedelta(seconds=heartbeat_timeout_s))
    rows = conn.execute(
        "SELECT * FROM leases WHERE released_at IS NULL"
        " AND (expires_at <= ? OR last_heartbeat <= ?) ORDER BY id",
        (now_text, heartbeat_floor),
    ).fetchall()
    return [_lease(row) for row in rows]


# --------------------------------------------------------------------------
# Events / audit
# --------------------------------------------------------------------------
def log_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    now: str,
    seat_id: int | None = None,
    request_id: int | None = None,
    agent_label: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (at, event_type, seat_id, request_id, agent_label, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (now, event_type, seat_id, request_id, agent_label, detail),
    )


def list_events(conn: sqlite3.Connection, *, limit: int | None = None) -> list[Event]:
    sql = "SELECT * FROM events ORDER BY id"
    params: list[object] = []
    if limit is not None:
        sql += " DESC LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    events = [_event(row) for row in rows]
    if limit is not None:
        events.reverse()
    return events


# --------------------------------------------------------------------------
# Store facade
# --------------------------------------------------------------------------
class StateStore:
    """Thread-safe handle to a state database.

    Reads open a short-lived connection; writes/compound operations use
    :meth:`transaction`. Connection-per-transaction means two processes (or
    threads) can safely share the same file and rely on sqlite's own
    ``BEGIN IMMEDIATE`` write lock for *row* atomicity (claiming, state
    transitions).

    This does **not** make the VM operations that span multiple transactions
    safe across processes: the per-seat/per-repo locks in
    :mod:`omavroom.manager.locks` are in-process only. A single daemon is
    assumed in Phase 4A; cross-process mutual exclusion for
    export/reset/release is deferred to 4B.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        return connect(self.path)

    def init(self) -> None:
        conn = self.connect()
        try:
            init_schema(conn)
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """Yield a write connection inside an immediate transaction."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def read(self, fn):
        """Run ``fn(conn)`` against a fresh read connection."""
        conn = self.connect()
        try:
            return fn(conn)
        finally:
            conn.close()
