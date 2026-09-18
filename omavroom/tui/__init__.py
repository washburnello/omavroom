"""Metadata-only Textual companion for omavroom (Phase 6).

One job: monitor the pool over SSH. By design it renders **no framebuffer
contents** — no Kitty/Sixel/iTerm2 image protocols — only an ASCII "monitor
wall" (fixed slots derived from settings), attachment labels, the queue
sidebar with next-up positions, and a needs-attention panel. The native GUI
(Phase 8) remains the only surface that renders monitor contents.

Stability contract
------------------
The slot set comes from ``per_type[*].max_seats`` (settings), never from live
seat counts. A VM leaving turns its slot "off / no signal" **in place**; slots
never appear or disappear on VM lifecycle events. Only a settings change (a
different daemon config ``max_seats``) rebuilds the wall.

Refresh model
-------------
- one snapshot on mount,
- an auto-refresh interval (``--interval``, default 2 s),
- ``r`` forces an immediate refresh,
- ``p``/``s`` act on the focused slot and only print an endpoint/path
  (never render an image); ``q`` quits.
- A down daemon shows "daemon not running" plus a retry hint and never raises;
  the next successful refresh clears it.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Static

from omavroom.client import DaemonClient, DaemonClientError
from omavroom.config import Config
from omavroom.poolview import (
    NO_SIGNAL,
    SEAT_TYPE_ORDER,
    SeatRow,
    Slot,
    assign_slots,
    build_seat_rows,
    build_waiter_rows,
    format_mb,
    headroom_mb,
    next_up_ids,
    now_utc,
    pool_totals,
    render_seat_label,
    render_slot,
    slot_plan,
)

DEFAULT_REFRESH_INTERVAL_S = 2.0
SIDEBAR_WIDTH = 44


def plan_from_config(config: Config) -> list[Slot]:
    """Fallback slot plan from the local config (used before the first poll)."""
    per_type = {
        seat_type: {"max_seats": config.seats[seat_type].max_seats}
        for seat_type in SEAT_TYPE_ORDER
        if seat_type in config.seats
    }
    return slot_plan(per_type)


class SlotWidget(Static):
    """One fixed monitor slot; its content changes, the slot does not."""

    can_focus = True

    def __init__(self, slot: Slot) -> None:
        # markup=False: seat/project labels are operator data, not markup.
        super().__init__(id=f"slot-{slot.key}", markup=False)
        self.slot = slot
        self.slot_text = f"{slot.name}\n{NO_SIGNAL}"
        self.update(self.slot_text)

    def show(self, row: SeatRow | None, *, now=None) -> None:
        self.slot_text = render_slot(self.slot, row, now=now)
        self.update(self.slot_text)


class OmavroomTUI(App[None]):
    """The monitor wall app; inject ``client_factory`` for tests."""

    TITLE = "omavroom"
    SUB_TITLE = "metadata-only monitor wall"
    CSS = f"""
    Screen {{ layout: vertical; }}
    #pool-bar {{ height: 1; background: $boost; padding: 0 1; }}
    Horizontal {{ height: 1fr; }}
    #wall {{ width: 1fr; }}
    #monitor-grid {{ grid-size: 2; grid-gutter: 1 1; padding: 0 1; }}
    #sidebar {{ width: {SIDEBAR_WIDTH}; padding: 0 1; }}
    SlotWidget {{ border: ascii $primary; padding: 0 1; min-height: 8; }}
    SlotWidget:focus {{ border: ascii $accent; }}
    #queue-panel, #attention-panel {{ border: ascii $secondary; padding: 0 1; margin-bottom: 1; }}
    #notice {{ color: $accent; padding: 0 1; }}
    #daemon-down {{ display: none; background: $error; color: $text; padding: 1 2; }}
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("p", "peek", "Peek"),
        Binding("s", "screenshot", "Screenshot"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        socket_path: str | Path | None = None,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL_S,
        client_factory: Callable[[], Any] | None = None,
        config: Config | None = None,
    ) -> None:
        super().__init__()
        self._socket_path = socket_path
        self.refresh_interval = refresh_interval
        self._config = config or Config.load()
        self._client_factory = client_factory or (lambda: DaemonClient(socket_path))
        self._client: Any = None
        self._status: dict | None = None
        self._plan: list[Slot] = plan_from_config(self._config)
        self._slots: dict[str, SlotWidget] = {}
        self._assignment: dict[str, SeatRow | None] = {}
        self._seat_slot_map: dict[str, int | None] = {}
        self.down_message: str | None = None

    # -- composition -----------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("connecting to daemon...", id="pool-bar", markup=False)
        yield Horizontal(
            VerticalScroll(Grid(id="monitor-grid"), id="wall"),
            VerticalScroll(
                Static("QUEUE\n(loading)", id="queue-panel", markup=False),
                Static("NEEDS ATTENTION\n(loading)", id="attention-panel", markup=False),
                Static("", id="notice", markup=False),
                id="sidebar",
            ),
        )
        yield Static(id="daemon-down", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._mount_slots(self._plan)
        self.refresh_data()
        self.set_interval(self.refresh_interval, self.refresh_data)

    # -- slot lifecycle --------------------------------------------------
    def _mount_slots(self, plan: list[Slot]) -> None:
        grid = self.query_one("#monitor-grid", Grid)
        for child in list(grid.children):
            child.remove()
        self._slots = {}
        columns = max(1, min(3, _ceil_sqrt(len(plan))))
        grid.styles.grid_size_columns = columns
        for slot in plan:
            widget = SlotWidget(slot)
            if slot.seat_type == "desktop" and columns > 1:
                widget.styles.column_span = min(2, columns)
            grid.mount(widget)
            self._slots[slot.key] = widget

    def _sync_plan(self, per_type: dict) -> None:
        plan = slot_plan(per_type) if per_type else self._plan
        if [slot.key for slot in plan] != [slot.key for slot in self._plan]:
            self._plan = plan
            self._mount_slots(plan)

    # -- data ------------------------------------------------------------
    def refresh_data(self) -> None:
        """Fetch one pool snapshot and repaint; never raises on a down daemon."""
        try:
            if self._client is None:
                self._client = self._client_factory()
            status = self._client.pool_status()
        except DaemonClientError as exc:
            self._show_down(str(exc))
            return
        self._clear_down()
        self._status = status
        self._sync_plan(status.get("per_type") or {})
        self._render(status)

    def _render(self, status: dict) -> None:
        now = now_utc()
        rows = build_seat_rows(status, now=now)
        self._assignment = assign_slots(self._plan, rows, previous=self._seat_slot_map)
        self._seat_slot_map = {
            key: (row.seat_id if row is not None else None) for key, row in self._assignment.items()
        }
        for slot in self._plan:
            widget = self._slots.get(slot.key)
            if widget is not None:
                widget.show(self._assignment.get(slot.key), now=now)

        per_type = status.get("per_type") or {}
        self.query_one("#pool-bar", Static).update(
            f"pool: {pool_totals(per_type)}"
            f" | free {format_mb(status.get('free_ram_mb'))}"
            f" | headroom {format_mb(headroom_mb(status))}"
            f" | admission {status.get('admission_override', '?')}"
        )
        self._render_queue(status, now)
        self._render_attention(rows)

    def _render_queue(self, status: dict, now) -> None:
        waiters = build_waiter_rows(status, now=now)
        next_ids = next_up_ids(waiters)
        lines = ["QUEUE"]
        if not waiters:
            lines.append("(no waiters)")
        for waiter in waiters:
            tag = "NEXT" if waiter.request_id in next_ids else f"#{waiter.position}"
            project = f" [{waiter.project}]" if waiter.project else ""
            lines.append(f"{tag:>4} {waiter.seat_type} {waiter.agent}{project}")
        self.query_one("#queue-panel", Static).update("\n".join(lines))

    def _render_attention(self, rows: list[SeatRow]) -> None:
        attention = [row for row in rows if row.needs_attention]
        lines = ["NEEDS ATTENTION"]
        if not attention:
            lines.append("(none)")
        lines.extend(f"  {render_seat_label(row)}" for row in attention)
        self.query_one("#attention-panel", Static).update("\n".join(lines))

    # -- daemon-down handling -------------------------------------------
    def _show_down(self, message: str) -> None:
        self.down_message = message
        if self._client is not None:
            self._client.close()
            self._client = None
        self.query_one("#pool-bar", Static).update("daemon not running")
        banner = self.query_one("#daemon-down", Static)
        banner.update(f"DAEMON NOT RUNNING\n{message}\n\nPress r to retry.")
        banner.display = True

    def _clear_down(self) -> None:
        self.down_message = None
        self.query_one("#daemon-down", Static).display = False

    def _notice(self, message: str) -> None:
        self.query_one("#notice", Static).update(message)
        self.notify(message)

    # -- actions ---------------------------------------------------------
    def action_refresh(self) -> None:
        self.refresh_data()

    def action_peek(self) -> None:
        self._slot_action("peek")

    def action_screenshot(self) -> None:
        self._slot_action("screenshot")

    def _slot_action(self, kind: str) -> None:
        focused = self.focused
        if not isinstance(focused, SlotWidget):
            self.notify("focus a monitor slot first")
            return
        row = self._assignment.get(focused.slot.key)
        if row is None:
            self.notify(f"{focused.slot.name}: no live seat")
            return
        if self._client is None:
            self.notify("daemon not running")
            return
        try:
            if kind == "peek":
                endpoint = self._client.peek_endpoint(row.seat_id)
                self._notice(f"{focused.slot.name} peek endpoint: {endpoint}")
            else:
                data = self._client.screenshot(row.seat_id)
                path = Path(tempfile.gettempdir()) / f"omavroom-{row.name}.png"
                path.write_bytes(data)
                self._notice(f"{focused.slot.name} screenshot path: {path}")
        except DaemonClientError as exc:
            self.notify(f"{kind} failed: {exc}", severity="error")


def _ceil_sqrt(count: int) -> int:
    if count <= 0:
        return 0
    root = 1
    while root * root < count:
        root += 1
    return root


def run_tui(
    *,
    socket_path: str | Path | None = None,
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL_S,
) -> int:
    """Run the TUI until the user quits; returns a process exit code."""
    app = OmavroomTUI(socket_path=socket_path, refresh_interval=refresh_interval)
    app.run()
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - console script
    """``omavroom-tui`` entry point."""
    import argparse

    parser = argparse.ArgumentParser(prog="omavroom-tui")
    parser.add_argument("--socket", default=None, help="override the daemon socket path")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_REFRESH_INTERVAL_S,
        help="auto-refresh seconds (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    return run_tui(socket_path=args.socket, refresh_interval=args.interval)


__all__ = ["OmavroomTUI", "SlotWidget", "main", "plan_from_config", "run_tui"]
