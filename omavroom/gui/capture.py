"""Pure adaptive-capture scheduler for the monitor wall (package A).

The wall runs two cadences:

- a *fast* tick (``focused_interval_s``) that captures only the focused
  desktop seat at ``focused_width``, so the enlarged monitor stays crisp and
  current; and
- a *slow* wall pass (``wall_interval_s``) that refreshes every other live
  desktop seat at the cheap ``thumbnail_width``.

The focused seat is deliberately excluded from the wall pass: it was just
captured at high resolution, so capturing it twice would be wasted work. When
focus moves or clears, the seat that lost focus simply reappears in the next
wall pass (the planner only excludes the *current* focused seat), so its tile
degrades back to a thumbnail without ever blanking.

:meth:`CapturePlanner.plan` is pure: it takes the current monotonic time and
the live seat rows and returns the captures to perform. Tracking "wall due" by
time (rather than counting ticks) means a slow capture that overruns several
fast ticks coalesces into a single wall pass instead of queueing unbounded
work. The clock is injectable so tests need no real time.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

#: Seat states whose framebuffer is worth capturing.
LIVE_STATES: frozenset[str] = frozenset({"ready", "busy"})

#: Hard floor on any capture width. The worker imports this so the config
#: floor, the CLI override floor and the worker clamp can never disagree.
MIN_WIDTH = 64
#: Hard ceiling on any capture width. The worker imports this so the config
#: ceiling, the CLI override ceiling and the worker clamp can never disagree.
MAX_WIDTH = 4096


@dataclass(frozen=True)
class Capture:
    """One seat framebuffer request: which seat, how wide, and why."""

    seat_id: int
    width: int
    focused: bool


@dataclass(frozen=True)
class CapturePlan:
    """What a single poll tick should capture.

    ``focused`` is the high-resolution focused-monitor capture (or ``None``);
    ``wall`` is the thumbnail pass over every *other* live desktop seat. The
    current focused seat is never present in both.
    """

    focused: Capture | None
    wall: tuple[Capture, ...]

    @property
    def captures(self) -> tuple[Capture, ...]:
        """Focused first, then the thumbnail pass (deduped by construction)."""
        if self.focused is None:
            return self.wall
        return (self.focused, *self.wall)


def _coerce_width(value: object) -> int:
    return max(MIN_WIDTH, min(MAX_WIDTH, int(value)))


def live_desktop_ids(seats: Iterable[object]) -> list[int]:
    """Seat ids of live desktop seats that can actually yield a framebuffer.

    A seat qualifies only when it is a desktop seat, in a live state
    (``ready``/``busy``), has a VM, and carries a numeric id. Malformed rows
    are skipped rather than raising.
    """
    ids: list[int] = []
    for seat in seats:
        if not isinstance(seat, Mapping):
            continue
        if str(seat.get("seat_type") or "") != "desktop":
            continue
        if str(seat.get("state") or "") not in LIVE_STATES:
            continue
        if not seat.get("vm_name"):
            continue
        try:
            ids.append(int(seat.get("id")))
        except (TypeError, ValueError):
            continue
    return ids


class CapturePlanner:
    """Decides, per tick, which seats to capture and at what width.

    Owned by the worker thread; pure apart from the injected clock. All widths
    and cadences are clamped to sane bounds so a bad settings value can never
    produce an unbounded or zero-size capture.
    """

    def __init__(
        self,
        *,
        thumbnail_width: int = 480,
        focused_width: int = 1024,
        focused_interval_s: float = 0.5,
        wall_interval_s: float = 2.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.thumbnail_width = _coerce_width(thumbnail_width)
        self.focused_width = _coerce_width(focused_width)
        self.focused_interval_s = max(0.05, float(focused_interval_s))
        self.wall_interval_s = max(self.focused_interval_s, float(wall_interval_s))
        self._clock = clock or time.monotonic
        self._focused_seat_id: int | None = None
        #: First tick is always a full wall pass.
        self._next_wall_at: float | None = None

    @property
    def focused_seat_id(self) -> int | None:
        return self._focused_seat_id

    def now(self) -> float:
        """Current time from the injected clock."""
        return float(self._clock())

    def set_focus(self, seat_id: int | None) -> None:
        """Focus a seat (or ``None`` to clear); no immediate capture here."""
        self._focused_seat_id = None if seat_id is None else int(seat_id)

    def wall_due(self, now: float) -> bool:
        """Whether the slow wall pass is due at monotonic time ``now``.

        Lets the worker refresh ``pool_status``/``list_execs`` on the wall
        cadence while fast ticks only capture the focused seat. Pure; does not
        advance the deadline (that is still :meth:`plan`'s job).
        """
        return self._next_wall_at is None or now >= self._next_wall_at

    def plan(self, now: float, seats: Iterable[object]) -> CapturePlan:
        """Return the captures due at monotonic time ``now`` for ``seats``."""
        live = live_desktop_ids(seats)
        focused: Capture | None = None
        if self._focused_seat_id is not None and self._focused_seat_id in live:
            focused = Capture(self._focused_seat_id, self.focused_width, True)

        wall: list[Capture] = []
        if self.wall_due(now):
            for seat_id in live:
                if focused is not None and seat_id == focused.seat_id:
                    continue
                wall.append(Capture(seat_id, self.thumbnail_width, False))
            # Advance from *now*, not from the stale deadline: a delayed tick
            # catches up with exactly one wall pass and cannot queue more.
            self._next_wall_at = now + self.wall_interval_s
        return CapturePlan(focused, tuple(wall))


__all__ = [
    "LIVE_STATES",
    "MAX_WIDTH",
    "MIN_WIDTH",
    "Capture",
    "CapturePlan",
    "CapturePlanner",
    "live_desktop_ids",
]
