"""Read model shared by the Phase 6 CLI and the metadata-only TUI companion.

Both surfaces are thin clients of the daemon's protocol-v1 ``pool_status``
JSON. This module converts that wire shape into small typed rows and pure
render helpers, so the CLI table and the Textual slot wall derive *identical*
state and can be unit-tested without a daemon or a terminal.

Design notes
------------
- No I/O and no daemon/client imports: everything here consumes the plain
  dicts/lists returned by :meth:`omavroom.client.DaemonClient.pool_status`.
- The slot set is derived only from ``per_type[*].max_seats`` (settings),
  never from live seat counts. That is what makes the "monitor wall" stable:
  a VM teardown turns a screen *off in place* instead of removing a slot.
- Rendering is text-only by construction; nothing here touches a framebuffer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: Canonical durable timestamp format (see :mod:`omavroom.state`).
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
#: Text shown for a slot with no live seat (the "screen off" state).
NO_SIGNAL = "off / no signal"
#: Preferred left-to-right ordering of seat types in the slot wall.
SEAT_TYPE_ORDER: tuple[str, ...] = ("desktop", "terminal")


def now_utc() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def parse_time(value: Any) -> datetime | None:
    """Parse a durable timestamp string; return ``None`` for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.strptime(value, TIME_FORMAT)
    except (ValueError, TypeError):
        return None
    return moment.replace(tzinfo=UTC)


def _as_utc(reference: datetime | None) -> datetime:
    moment = reference or now_utc()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def format_elapsed(seconds: float | None) -> str:
    """``HH:MM:SS`` / ``MM:SS`` for an elapsed duration, ``-`` when unknown."""
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_short(seconds: float | None) -> str:
    """Compact duration (``1h02m`` / ``12m34s`` / ``45s``)."""
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def elapsed_since(value: Any, now: datetime | None = None) -> float | None:
    """Seconds since a timestamp, or ``None`` when it is missing/unknown."""
    moment = parse_time(value)
    if moment is None:
        return None
    return max(0.0, (_as_utc(now) - moment).total_seconds())


def format_lease(expires_at: Any, now: datetime | None = None) -> str:
    """Time left on a lease (``12m34s left`` / ``expired`` / ``-``)."""
    moment = parse_time(expires_at)
    if moment is None:
        return "-"
    remaining = (moment - _as_utc(now)).total_seconds()
    if remaining <= 0:
        return "expired"
    return f"{format_short(remaining)} left"


def format_mb(megabytes: int | float | None) -> str:
    """Human binary size for a MiB value."""
    if megabytes is None:
        return "-"
    value = float(megabytes)
    for unit in ("MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "MiB" else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover - defensive


@dataclass(frozen=True)
class SeatRow:
    """One live seat, joined with its claimed request for project/elapsed."""

    seat_id: int
    name: str
    seat_type: str
    state: str
    agent: str
    project: str | None
    image: str
    vm_name: str | None
    elapsed_s: float | None
    lease_expires_at: str | None
    last_heartbeat: str | None
    needs_attention: bool
    last_error: str | None


@dataclass(frozen=True)
class WaiterRow:
    """One queued (unclaimed) request."""

    request_id: int
    position: int
    seat_type: str
    agent: str
    project: str | None
    image: str
    waited_s: float | None
    queue_ahead: int


@dataclass(frozen=True)
class Slot:
    """A fixed monitor slot; ``index`` is zero-based within its seat type."""

    seat_type: str
    index: int

    @property
    def key(self) -> str:
        return f"{self.seat_type}-{self.index}"

    @property
    def name(self) -> str:
        return f"{self.seat_type}-{self.index + 1}"


def _seat_type_sort_key(seat_type: str) -> tuple[int, str]:
    try:
        return (SEAT_TYPE_ORDER.index(seat_type), seat_type)
    except ValueError:
        return (len(SEAT_TYPE_ORDER), seat_type)


def _int_or(value: Any, default: int) -> int:
    """Coerce ``value`` to int, or return ``default`` for anything unusable.

    Defensive companion to :func:`build_seat_rows`: a malformed payload (a
    string id, ``None``, a nested object) must not raise on a monitoring path.
    Bools are rejected (``True`` is not seat id 1).
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_seat_rows(status: dict, *, now: datetime | None = None) -> list[SeatRow]:
    """Join ``pool_status.seats`` with claimed requests for project/elapsed.

    Tolerant by construction: a non-dict ``status`` or a seat record with a
    missing/non-int ``id`` is skipped rather than raising, so a malformed
    daemon payload can only degrade the view, never crash a monitor.
    """
    if not isinstance(status, dict):
        return []
    seats = status.get("seats") or []
    queue = status.get("queue") or []
    if not isinstance(seats, list):
        seats = []
    if not isinstance(queue, list):
        queue = []
    claimed = {
        _int_or(request.get("seat_id"), -1): request
        for request in queue
        if isinstance(request, dict)
        and request.get("seat_id") is not None
        and request.get("status") == "claimed"
    }
    rows: list[SeatRow] = []
    for seat in seats:
        if not isinstance(seat, dict):
            continue
        raw_id = seat.get("id")
        if isinstance(raw_id, bool) or raw_id is None:
            continue
        try:
            seat_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        request = claimed.get(seat_id) or {}
        lease = request.get("lease") or {}
        rows.append(
            SeatRow(
                seat_id=seat_id,
                name=str(seat.get("name") or seat_id),
                seat_type=str(seat.get("seat_type") or "?"),
                state=str(seat.get("state") or "?"),
                agent=str(seat.get("agent_label") or "-"),
                project=request.get("project"),
                image=str(seat.get("image") or "-"),
                vm_name=seat.get("vm_name"),
                elapsed_s=elapsed_since(lease.get("acquired_at"), now),
                lease_expires_at=seat.get("lease_expires_at"),
                last_heartbeat=seat.get("last_heartbeat"),
                needs_attention=bool(seat.get("needs_attention")),
                last_error=seat.get("last_error"),
            )
        )
    rows.sort(key=lambda row: (_seat_type_sort_key(row.seat_type), row.seat_id))
    return rows


def build_waiter_rows(status: dict, *, now: datetime | None = None) -> list[WaiterRow]:
    """Queued requests, sorted by seat type then scheduler position.

    Like :func:`build_seat_rows`, this tolerates a non-dict ``status`` and
    skips malformed request records instead of raising.
    """
    if not isinstance(status, dict):
        return []
    queue = status.get("queue") or []
    if not isinstance(queue, list):
        return []
    rows = [
        WaiterRow(
            request_id=_int_or(request.get("id"), 0),
            position=_int_or(request.get("position"), 0),
            seat_type=str(request.get("seat_type") or "?"),
            agent=str(request.get("agent_label") or "-"),
            project=request.get("project"),
            image=str(request.get("image") or "-"),
            waited_s=elapsed_since(request.get("created_at"), now),
            queue_ahead=_int_or(request.get("queue_ahead"), 0),
        )
        for request in queue
        if isinstance(request, dict) and request.get("status") == "waiting"
    ]
    rows.sort(key=lambda row: (_seat_type_sort_key(row.seat_type), row.position, row.request_id))
    return rows


def next_up_ids(waiters: list[WaiterRow]) -> set[int]:
    """Request ids that are next in line (minimum position per seat type)."""
    seen: set[str] = set()
    next_ids: set[int] = set()
    for waiter in waiters:
        if waiter.seat_type in seen:
            continue
        seen.add(waiter.seat_type)
        next_ids.add(waiter.request_id)
    return next_ids


def slot_plan(per_type: dict) -> list[Slot]:
    """Fixed monitor slots from ``per_type[*].max_seats`` (settings only)."""
    ordered = sorted(per_type or {}, key=_seat_type_sort_key)
    slots: list[Slot] = []
    for seat_type in ordered:
        info = per_type.get(seat_type) or {}
        try:
            count = int(info.get("max_seats") or 0)
        except (TypeError, ValueError):
            count = 0
        slots.extend(Slot(seat_type, index) for index in range(max(0, count)))
    return slots


def assign_slots(
    slots: list[Slot],
    rows: list[SeatRow],
    *,
    previous: dict[str, int | None] | None = None,
) -> dict[str, SeatRow | None]:
    """Attach live seats to fixed slots without ever reflowing occupied slots.

    ``previous`` maps a slot key to the seat id it held last refresh. A seat
    that is still live keeps its slot; freed slots are refilled (by ascending
    seat id, lowest free index within the same seat type) and every other
    slot renders "no signal". Passing the prior assignment is what guarantees
    the PLAN.md contract: a VM exit turns a screen off *in place* and never
    moves another seat's monitor.
    """
    live = {row.seat_id: row for row in rows}
    assignment: dict[str, SeatRow | None] = {}
    placed: set[int] = set()
    for slot in slots:
        seat_id = (previous or {}).get(slot.key)
        row = live.get(seat_id) if seat_id is not None else None
        if row is not None and row.seat_type == slot.seat_type:
            assignment[slot.key] = row
            placed.add(row.seat_id)

    free_by_type: dict[str, list[Slot]] = {}
    for slot in slots:
        if slot.key not in assignment:
            free_by_type.setdefault(slot.seat_type, []).append(slot)

    for row in sorted(
        (candidate for candidate in rows if candidate.seat_id not in placed),
        key=lambda candidate: candidate.seat_id,
    ):
        candidates = free_by_type.get(row.seat_type) or []
        if not candidates:
            continue
        slot = candidates.pop(0)
        assignment[slot.key] = row
        placed.add(row.seat_id)

    for slot in slots:
        assignment.setdefault(slot.key, None)
    return assignment


def render_slot(slot: Slot, row: SeatRow | None, *, now: datetime | None = None) -> str:
    """Plain-text contents of one monitor slot (never framebuffer data)."""
    if row is None:
        return f"{slot.name}\n{NO_SIGNAL}"
    project = row.project or "-"
    return (
        f"{slot.name}\n"
        f"agent    {row.agent}\n"
        f"project  {project}\n"
        f"state    {row.state}\n"
        f"elapsed  {format_elapsed(row.elapsed_s)}\n"
        f"lease    {format_lease(row.lease_expires_at, now)}"
    )


def render_seat_label(row: SeatRow) -> str:
    """One-line description for the needs-attention panel."""
    detail = f": {row.last_error}" if row.last_error else ""
    return f"{row.name} ({row.seat_type}) {row.state} agent={row.agent}{detail}"


def pool_totals(per_type: dict) -> str:
    """``desktop 1/1  terminal 0/2`` from authoritative per-type counts."""
    parts: list[str] = []
    for seat_type in sorted(per_type or {}, key=_seat_type_sort_key):
        info = per_type.get(seat_type) or {}
        occupying = info.get("occupying", 0)
        maximum = info.get("max_seats", 0)
        waiting = info.get("waiting", 0)
        suffix = f", {waiting} waiting" if waiting else ""
        parts.append(f"{seat_type} {occupying}/{maximum}{suffix}")
    return "  ".join(parts) if parts else "no seat types configured"


def headroom_mb(status: dict) -> int:
    """Free RAM above the configured headroom floor (the admission budget)."""
    free = int(status.get("free_ram_mb") or 0)
    floor = int(status.get("headroom_floor_mb") or 0)
    return free - floor


__all__ = [
    "NO_SIGNAL",
    "SEAT_TYPE_ORDER",
    "TIME_FORMAT",
    "SeatRow",
    "Slot",
    "WaiterRow",
    "assign_slots",
    "build_seat_rows",
    "build_waiter_rows",
    "elapsed_since",
    "format_elapsed",
    "format_lease",
    "format_mb",
    "format_short",
    "headroom_mb",
    "next_up_ids",
    "now_utc",
    "parse_time",
    "pool_totals",
    "render_seat_label",
    "render_slot",
    "slot_plan",
]
