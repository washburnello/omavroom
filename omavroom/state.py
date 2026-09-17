"""SQLite state helpers (Phase 0 stub).

The Phase 4 scheduler owns the full schema and all queries; this module
only provides the connection defaults every later phase relies on
(WAL journal mode so readers never block the daemon writer, foreign keys
on) plus `init_schema`, which creates the three core tables —
`seats`, `leases`, `queue` — so later phases can build on a stable shape.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    seat_type TEXT NOT NULL CHECK (seat_type IN ('desktop', 'terminal')),
    state TEXT NOT NULL DEFAULT 'off',
    vm_name TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seat_id INTEGER NOT NULL REFERENCES seats (id) ON DELETE CASCADE,
    agent_label TEXT NOT NULL,
    acquired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_label TEXT NOT NULL,
    seat_type TEXT NOT NULL CHECK (seat_type IN ('desktop', 'terminal')),
    status TEXT NOT NULL DEFAULT 'waiting',
    requested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the state database with the project's standard pragmas.

    WAL mode lets future monitor/TUI readers query while the manager
    daemon writes; `foreign_keys` keeps leases/queue rows tied to seats.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the `seats`, `leases`, and `queue` tables if missing."""
    conn.executescript(SCHEMA)


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return the user-table names present in the connected database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]
