"""Pure, Qt-free view model for the native Command Center (Phase 8).

This is the testable heart of the GUI: it turns one protocol-v1
``pool_status`` snapshot (plus per-seat ``list_execs`` rows and downscaled
screenshot bytes) into plain, JSON-ready structures that the thin Qt layer
simply renders. There is **no** PySide6 import here, so the whole module is
unit-testable with plain ``pytest``.

Stability contract (same as the Phase 6 TUI)
--------------------------------------------
The slot set comes from settings (``per_type[*].max_seats``), never from live
seat counts. :func:`omavroom.poolview.assign_slots` is reused verbatim, so a
VM teardown turns its slot "off / no signal" in place and never moves an
occupied slot. Only a settings change rebuilds the wall.

What the GUI adds over the TUI
-----------------------------
- Graphical (desktop) slots carry a base64 PNG ``thumbnail_source`` captured
  from the daemon; terminal slots carry a text ``terminal_text`` tail derived
  from ``list_execs`` (the most recent exec plus its output tail).
- A pure :func:`grid_columns` packing rule gives the responsive wall its
  column count without any widget math.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from omavroom.config import Config
from omavroom.poolview import (
    NO_SIGNAL,
    SEAT_TYPE_ORDER,
    SeatRow,
    Slot,
    assign_slots,
    build_seat_rows,
    build_waiter_rows,
    elapsed_since,
    format_elapsed,
    format_lease,
    format_mb,
    format_short,
    next_up_ids,
    now_utc,
    pool_totals,
    slot_plan,
)

#: Shown in a terminal slot with no exec activity at all.
TERMINAL_IDLE = "idle"
#: How much exec output a terminal slot shows before trimming the oldest lines.
TERMINAL_TAIL_LINES = 12
TERMINAL_TAIL_CHARS = 1600
#: Responsive packing: one wall column per this many horizontal pixels.
DEFAULT_UNIT_PX = 260
DEFAULT_MAX_COLUMNS = 4
#: Recovery actions offered for a needs-attention seat (ids match the daemon).
ATTENTION_ACTIONS: tuple[dict[str, str], ...] = (
    {"id": "retry-release", "label": "Retry release"},
    {"id": "force-discard", "label": "Force discard"},
    {"id": "destroy", "label": "Destroy"},
)
#: Only desktop (graphical) seats have a viewer endpoint. Terminal seats are
#: headless, so offering click-to-peek on them is an affordance that can only
#: dead-end; the wall gates the cursor/tooltip/click on this.
PEEKABLE_SEAT_TYPES: frozenset[str] = frozenset({"desktop"})


def is_peekable(seat_type: str) -> bool:
    """Whether a seat type has a viewer endpoint worth offering."""
    return seat_type in PEEKABLE_SEAT_TYPES


@dataclass(frozen=True)
class SlotState:
    """One permanent monitor slot, ready for QML (all values primitive)."""

    key: str
    name: str
    seat_type: str
    index: int
    occupied: bool
    seat_id: int | None
    agent: str
    project: str
    state: str
    image: str
    elapsed: str
    lease: str
    heartbeat: str
    needs_attention: bool
    last_error: str
    terminal_text: str
    thumbnail_source: str
    off_text: str
    #: Whether this slot offers click-to-peek (desktop seats only).
    peekable: bool


@dataclass(frozen=True)
class WaiterState:
    """One queued request as shown in the queue sidebar."""

    request_id: int
    position: int
    seat_type: str
    agent: str
    project: str
    image: str
    waited: str
    next_up: bool
    queue_ahead: int


@dataclass(frozen=True)
class AttentionState:
    """One held/unrecoverable seat plus the operator actions available."""

    seat_id: int
    name: str
    seat_type: str
    state: str
    agent: str
    project: str
    last_error: str
    actions: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class WallState:
    """The whole monitor wall + queue + attention + pool summary."""

    slots: tuple[SlotState, ...]
    waiters: tuple[WaiterState, ...]
    attention: tuple[AttentionState, ...]
    per_type: dict
    free_ram_mb: int | None
    headroom_mb: int | None
    admission_override: str
    pool_text: str
    free_text: str
    headroom_text: str

    @property
    def slot_keys(self) -> list[str]:
        return [slot.key for slot in self.slots]

    def slot_at(self, index: int) -> dict:
        if 0 <= index < len(self.slots):
            return asdict(self.slots[index])
        return {}

    def waiter_dicts(self) -> list[dict]:
        return [asdict(waiter) for waiter in self.waiters]

    def attention_dicts(self) -> list[dict]:
        return [asdict(item) for item in self.attention]


def plan_from_config(config: Config) -> list[Slot]:
    """Fallback wall plan from the local config (used before the first poll)."""
    per_type = {
        seat_type: {"max_seats": config.seats[seat_type].max_seats}
        for seat_type in SEAT_TYPE_ORDER
        if seat_type in config.seats
    }
    return slot_plan(per_type)


def grid_columns(
    viewport_width: int,
    *,
    unit_px: int = DEFAULT_UNIT_PX,
    min_columns: int = 1,
    max_columns: int = DEFAULT_MAX_COLUMNS,
) -> int:
    """Responsive wall column count for a viewport width.

    Pure and testable: one column per ``unit_px`` of width, clamped to
    ``[min_columns, max_columns]``. Only window resizes feed this, so VM
    lifecycle never repacks the wall.
    """
    if viewport_width <= 0:
        return min_columns
    width = max(1, int(viewport_width))
    unit = max(1, int(unit_px))
    return max(min_columns, min(max_columns, width // unit))


def tile_column_span(seat_type: str, columns: int) -> int:
    """Desktop tiles are 2 columns wide (when the wall allows); terminals 1."""
    if seat_type == "desktop":
        return min(2, max(1, int(columns)))
    return 1


def tile_row_span(seat_type: str) -> int:
    """Desktop tiles are 2 rows tall (big ~16:9 screens); terminals compact."""
    return 2 if seat_type == "desktop" else 1


def heartbeat_text(last_heartbeat: Any, *, now: Any = None, timeout_s: int = 0) -> str:
    """``ok 12s`` / ``stale 6m00s`` / ``-`` for a seat's last heartbeat."""
    age = elapsed_since(last_heartbeat, now)
    if age is None:
        return "-"
    if timeout_s and age > timeout_s:
        return f"stale {format_short(age)}"
    return f"ok {format_short(age)}"


def _exec_line(exec_row: dict) -> str:
    label = exec_row.get("label") or exec_row.get("command") or exec_row.get("exec_id") or "exec"
    state = exec_row.get("state") or "?"
    if state == "running":
        return f"{label} (running)"
    if state == "killed":
        return f"{label} (killed)"
    exit_code = exec_row.get("exit_code")
    if exit_code is None:
        return f"{label} (finished)"
    return f"{label} (exit {exit_code})"


def _int_or_none(value: Any) -> int | None:
    """Coerce to int, or ``None`` for anything non-numeric.

    Defensive companion for pool-summary fields: a malformed payload (``None``,
    a string, a nested object) must degrade to "unknown" rather than raising.
    Bools are rejected, matching seat-row parsing.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str:
    """Coerce exec output to text; non-strings become their ``str()``.

    A malformed payload (``None``, a number, a nested object) must not raise
    deep in a render path, so anything unusable is stringified; ``None`` and
    empty values become ``""``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return str(value)


def _tail(
    text: str, *, max_lines: int = TERMINAL_TAIL_LINES, max_chars: int = TERMINAL_TAIL_CHARS
) -> str:
    if len(text) > max_chars:
        text = text[-max_chars:]
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def render_terminal_text(execs: list[dict] | None) -> str:
    """Terminal-slot "live text": the most recent exec and its output tail.

    Prefers a currently running exec (the active one); otherwise the most
    recently started record. Returns :data:`TERMINAL_IDLE` when the seat has
    no exec history at all. Non-dict records are skipped, and non-string
    ``stdout``/``stderr`` are coerced, so a malformed payload degrades to
    less text rather than raising.
    """
    if not execs:
        return TERMINAL_IDLE
    records = [item for item in execs if isinstance(item, dict)]
    if not records:
        return TERMINAL_IDLE
    ordered = sorted(records, key=lambda item: str(item.get("started_at") or ""))
    running = [item for item in ordered if item.get("state") == "running"]
    latest = running[-1] if running else ordered[-1]
    stdout = _as_text(latest.get("stdout"))
    stderr = _as_text(latest.get("stderr"))
    body = stdout
    if stderr:
        body = f"{body}\n{stderr}" if body else stderr
    body = body.strip("\n")
    if latest.get("truncated"):
        body = "...[truncated]\n" + body if body else "...[truncated]"
    body = _tail(body)
    header = _exec_line(latest)
    return f"{header}\n{body}" if body else header


class MonitorWall:
    """Holds the fixed plan, sticky seat assignment, and last rendered state.

    Pure and daemon-free; the Qt backend owns one instance and calls
    :meth:`update` once per poll.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.plan: list[Slot] = plan_from_config(config)
        self._previous: dict[str, int | None] = {}
        self.state: WallState = self.initial_state()

    def sync_plan(self, per_type: dict) -> bool:
        """Adopt a daemon-reported plan; return ``True`` when it changed."""
        if not per_type or not isinstance(per_type, dict):
            return False
        plan = slot_plan(per_type)
        if [slot.key for slot in plan] != [slot.key for slot in self.plan]:
            self.plan = plan
            self._previous = {}
            return True
        return False

    def initial_state(self) -> WallState:
        """All slots off, no queue/attention, before the first daemon poll."""
        slots = tuple(
            self._slot_state(slot, None, {}, {}, self.config.leases.heartbeat_timeout_s, now_utc())
            for slot in self.plan
        )
        return WallState(
            slots=slots,
            waiters=(),
            attention=(),
            per_type={},
            free_ram_mb=0,
            headroom_mb=0,
            admission_override="auto",
            pool_text="",
            free_text="-",
            headroom_text="-",
        )

    def update(
        self,
        status: dict,
        *,
        execs_by_seat: dict[int, list[dict]] | None = None,
        screenshots: dict[int, str] | None = None,
        now: Any = None,
    ) -> WallState:
        """Fold one pool snapshot into the wall; returns the new state."""
        moment = now or now_utc()
        if not isinstance(status, dict):
            status = {}
        rows = build_seat_rows(status, now=moment)
        assignment = assign_slots(self.plan, rows, previous=self._previous)
        self._previous = {
            key: (row.seat_id if row is not None else None) for key, row in assignment.items()
        }
        if not isinstance(execs_by_seat, dict):
            execs_by_seat = {}
        if not isinstance(screenshots, dict):
            screenshots = {}
        timeout_s = self.config.leases.heartbeat_timeout_s
        slots = tuple(
            self._slot_state(
                slot,
                assignment.get(slot.key),
                execs_by_seat,
                screenshots,
                timeout_s,
                moment,
            )
            for slot in self.plan
        )
        waiter_rows = build_waiter_rows(status, now=moment)
        next_ids = next_up_ids(waiter_rows)
        waiters = tuple(
            WaiterState(
                request_id=waiter.request_id,
                position=waiter.position,
                seat_type=waiter.seat_type,
                agent=waiter.agent,
                project=waiter.project or "-",
                image=waiter.image,
                waited=format_short(waiter.waited_s),
                next_up=waiter.request_id in next_ids,
                queue_ahead=waiter.queue_ahead,
            )
            for waiter in waiter_rows
        )
        attention = tuple(
            AttentionState(
                seat_id=row.seat_id,
                name=row.name,
                seat_type=row.seat_type,
                state=row.state,
                agent=row.agent,
                project=row.project or "-",
                last_error=row.last_error or "",
                actions=ATTENTION_ACTIONS,
            )
            for row in rows
            if row.needs_attention
        )
        per_type = status.get("per_type")
        if not isinstance(per_type, dict):
            per_type = {}
        free_ram_mb = _int_or_none(status.get("free_ram_mb"))
        floor_mb = _int_or_none(status.get("headroom_floor_mb"))
        headroom = (
            free_ram_mb - floor_mb if free_ram_mb is not None and floor_mb is not None else None
        )
        self.state = WallState(
            slots=slots,
            waiters=waiters,
            attention=attention,
            per_type=per_type,
            free_ram_mb=free_ram_mb,
            headroom_mb=headroom,
            admission_override=str(status.get("admission_override") or "auto"),
            pool_text=pool_totals(per_type),
            free_text=format_mb(free_ram_mb),
            headroom_text=format_mb(headroom),
        )
        return self.state

    def _slot_state(
        self,
        slot: Slot,
        row: SeatRow | None,
        execs_by_seat: dict[int, list[dict]],
        screenshots: dict[int, str],
        timeout_s: int,
        now: Any,
    ) -> SlotState:
        if row is None:
            return SlotState(
                key=slot.key,
                name=slot.name,
                seat_type=slot.seat_type,
                index=slot.index,
                occupied=False,
                seat_id=None,
                agent="-",
                project="-",
                state="off",
                image="-",
                elapsed="-",
                lease="-",
                heartbeat="-",
                needs_attention=False,
                last_error="",
                terminal_text="",
                thumbnail_source="",
                off_text=NO_SIGNAL,
                peekable=False,
            )
        terminal_text = ""
        if row.seat_type == "terminal":
            seat_execs = execs_by_seat.get(row.seat_id)
            terminal_text = render_terminal_text(seat_execs if isinstance(seat_execs, list) else [])
        thumbnail = screenshots.get(row.seat_id)
        thumbnail_source = (
            f"data:image/png;base64,{thumbnail}" if isinstance(thumbnail, str) and thumbnail else ""
        )
        return SlotState(
            key=slot.key,
            name=slot.name,
            seat_type=slot.seat_type,
            index=slot.index,
            occupied=True,
            seat_id=row.seat_id,
            agent=row.agent,
            project=row.project or "-",
            state=row.state,
            image=row.image,
            elapsed=format_elapsed(row.elapsed_s),
            lease=format_lease(row.lease_expires_at, now),
            heartbeat=heartbeat_text(row.last_heartbeat, now=now, timeout_s=timeout_s),
            needs_attention=row.needs_attention,
            last_error=row.last_error or "",
            terminal_text=terminal_text,
            thumbnail_source=thumbnail_source,
            off_text=NO_SIGNAL,
            peekable=is_peekable(row.seat_type),
        )


__all__ = [
    "ATTENTION_ACTIONS",
    "DEFAULT_MAX_COLUMNS",
    "DEFAULT_UNIT_PX",
    "PEEKABLE_SEAT_TYPES",
    "TERMINAL_IDLE",
    "TERMINAL_TAIL_CHARS",
    "TERMINAL_TAIL_LINES",
    "AttentionState",
    "MonitorWall",
    "SlotState",
    "WaiterState",
    "WallState",
    "grid_columns",
    "heartbeat_text",
    "is_peekable",
    "plan_from_config",
    "render_terminal_text",
    "tile_column_span",
    "tile_row_span",
]
