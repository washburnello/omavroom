"""Plain-text table/report rendering for the Phase 6 CLI.

Deliberately dependency-free (no rich/Textual import) so ``omavroom status``
is fast and its output is deterministic enough to assert on in tests. The
TUI renders the same :mod:`omavroom.poolview` rows into widgets instead.
"""

from __future__ import annotations

from datetime import datetime

from omavroom.config import Config
from omavroom.poolview import (
    SeatRow,
    WaiterRow,
    build_seat_rows,
    build_waiter_rows,
    format_elapsed,
    format_lease,
    format_mb,
    format_short,
    headroom_mb,
    next_up_ids,
    pool_totals,
)


def render_table(headers: list[str], rows: list[list[object]]) -> str:
    """Render a simple left-aligned, two-space separated text table."""
    columns = [[str(header) for header in headers]]
    for row in rows:
        columns.append(["" if value is None else str(value) for value in row])
    widths = [max(len(entry) for entry in column) for column in zip(*columns)]
    lines = [
        "  ".join(entry.ljust(width) for entry, width in zip(column, widths)).rstrip()
        for column in columns
    ]
    return "\n".join(lines)


def seats_table(rows: list[SeatRow], *, now: datetime | None = None) -> str:
    headers = ["NAME", "TYPE", "AGENT", "PROJECT", "STATE", "ELAPSED", "LEASE"]
    body = [
        [
            row.name,
            row.seat_type,
            row.agent,
            row.project or "-",
            row.state,
            format_elapsed(row.elapsed_s),
            format_lease(row.lease_expires_at, now),
        ]
        for row in rows
    ]
    return render_table(headers, body)


def queue_table(waiters: list[WaiterRow], *, now: datetime | None = None) -> str:
    headers = ["POS", "TYPE", "AGENT", "PROJECT", "IMAGE", "WAITED", "AHEAD"]
    next_ids = next_up_ids(waiters)
    body = [
        [
            waiter.position,
            waiter.seat_type,
            waiter.agent,
            waiter.project or "-",
            waiter.image,
            format_short(waiter.waited_s),
            "NEXT" if waiter.request_id in next_ids else waiter.queue_ahead,
        ]
        for waiter in waiters
    ]
    return render_table(headers, body)


def attention_lines(rows: list[SeatRow]) -> list[str]:
    """Human lines for seats an operator must act on (held/unrecoverable)."""
    from omavroom.poolview import render_seat_label

    return [render_seat_label(row) for row in rows if row.needs_attention]


def status_report(status: dict, *, now: datetime | None = None) -> str:
    """Full ``omavroom status`` text for one pool snapshot."""
    per_type = status.get("per_type") or {}
    seats = build_seat_rows(status, now=now)
    waiters = build_waiter_rows(status, now=now)
    attention = attention_lines(seats)

    lines = [
        (
            f"pool: free {format_mb(status.get('free_ram_mb'))}"
            f" | headroom {format_mb(status.get('headroom_floor_mb'))}"
            f" | admission {status.get('admission_override', '?')}"
            f" | auto-admit budget {format_mb(headroom_mb(status))}"
        ),
        f"seats: {pool_totals(per_type)}",
        "",
        "SEATS",
        seats_table(seats, now=now) if seats else "(none)",
    ]
    for seat_type, info in per_type.items():
        decision = "admit" if info.get("admitted") else "hold"
        lines.append(
            f"  {seat_type}: {decision} ({info.get('reason', '?')}),"
            f" occ {info.get('occupying', 0)}/{info.get('max_seats', 0)},"
            f" waiting {info.get('waiting', 0)}, errors {info.get('error_seats', 0)}"
        )
    lines += [
        "",
        "QUEUE",
        queue_table(waiters, now=now) if waiters else "(empty)",
        "",
        "NEEDS ATTENTION",
        "\n".join(f"  {line}" for line in attention) if attention else "(none)",
    ]
    return "\n".join(lines)


def settings_dict(config: Config) -> dict:
    """Effective config as a plain JSON-ready dict (read-only, no daemon)."""
    return {
        "capacity": {"total_units": config.capacity.total_units},
        "seats": {
            seat_type: {
                "cost_units": seat.cost_units,
                "min_seats": seat.min_seats,
                "max_seats": seat.max_seats,
                "image": seat.image,
            }
            for seat_type, seat in config.seats.items()
        },
        "resources": {
            seat_type: {
                "cpu_vcpus": resource.cpu_vcpus,
                "memory_mb": resource.memory_mb,
                "overlay_max_gb": resource.overlay_max_gb,
            }
            for seat_type, resource in config.resources.items()
        },
        "host": {"headroom_floor_mb": config.host.headroom_floor_mb},
        "admission": {
            "dynamic": config.admission.dynamic,
            "override": config.admission.override,
        },
        "leases": {
            "lease_timeout_s": config.leases.lease_timeout_s,
            "heartbeat_interval_s": config.leases.heartbeat_interval_s,
            "heartbeat_timeout_s": config.leases.heartbeat_timeout_s,
            "held_ttl_s": config.leases.held_ttl_s,
        },
        "exec": {
            "max_output_bytes": config.exec.max_output_bytes,
            "max_runtime_s": config.exec.max_runtime_s,
            "max_concurrent_per_seat": config.exec.max_concurrent_per_seat,
            "max_concurrent_total": config.exec.max_concurrent_total,
        },
        "export": {
            "max_files_changed": config.export.max_files_changed,
            "max_insertions": config.export.max_insertions,
            "max_deletions": config.export.max_deletions,
            "protected_paths": list(config.export.protected_paths),
            "approval_required": config.export.approval_required,
            "allowed_remotes": list(config.export.allowed_remotes),
        },
        "prewarm": {
            "max_retries": config.prewarm.max_retries,
            "backoff_s": config.prewarm.backoff_s,
        },
        "golden": {"profile": config.golden.profile},
        "gui": {
            "thumbnail_width": config.gui.thumbnail_width,
            "focused_width": config.gui.focused_width,
            "focused_interval_s": config.gui.focused_interval_s,
            "wall_interval_s": config.gui.wall_interval_s,
            "live_mode": config.gui.live_mode,
        },
    }


def settings_report(config: Config) -> str:
    """Human ``omavroom settings`` text for the effective merged config."""
    snapshot = settings_dict(config)
    seats = render_table(
        ["TYPE", "COST", "MIN", "MAX", "IMAGE"],
        [
            [
                seat_type,
                info["cost_units"],
                info["min_seats"],
                info["max_seats"],
                info["image"],
            ]
            for seat_type, info in snapshot["seats"].items()
        ],
    )
    resources = render_table(
        ["TYPE", "VCPUS", "MEM_MB", "OVERLAY_GB"],
        [
            [seat_type, info["cpu_vcpus"], info["memory_mb"], info["overlay_max_gb"]]
            for seat_type, info in snapshot["resources"].items()
        ],
    )
    leases = snapshot["leases"]
    execution = snapshot["exec"]
    export = snapshot["export"]
    host = snapshot["host"]
    admission = snapshot["admission"]
    golden = snapshot["golden"]
    gui = snapshot["gui"]
    return "\n".join(
        [
            f"capacity: total_units={snapshot['capacity']['total_units']}",
            "",
            "SEATS",
            seats,
            "",
            "RESOURCES",
            resources,
            "",
            f"host: headroom_floor_mb={host['headroom_floor_mb']}",
            f"admission: dynamic={str(admission['dynamic']).lower()}"
            f" override={admission['override']}",
            (
                f"leases: lease_timeout_s={leases['lease_timeout_s']}"
                f" heartbeat_interval_s={leases['heartbeat_interval_s']}"
                f" heartbeat_timeout_s={leases['heartbeat_timeout_s']}"
                f" held_ttl_s={leases['held_ttl_s']}"
            ),
            (
                f"exec: max_output_bytes={execution['max_output_bytes']}"
                f" max_runtime_s={execution['max_runtime_s']}"
                f" max_concurrent_per_seat={execution['max_concurrent_per_seat']}"
                f" max_concurrent_total={execution['max_concurrent_total']}"
            ),
            (
                f"export: max_files_changed={export['max_files_changed']}"
                f" max_insertions={export['max_insertions']}"
                f" max_deletions={export['max_deletions']}"
                f" approval_required={str(export['approval_required']).lower()}"
                f" protected_paths={','.join(export['protected_paths']) or '-'}"
            ),
            f"prewarm: max_retries={snapshot['prewarm']['max_retries']}"
            f" backoff_s={snapshot['prewarm']['backoff_s']}",
            f"golden: profile={golden['profile']}",
            (
                f"gui: thumbnail_width={gui['thumbnail_width']}"
                f" focused_width={gui['focused_width']}"
                f" focused_interval_s={gui['focused_interval_s']:g}"
                f" wall_interval_s={gui['wall_interval_s']:g}"
                f" live_mode={gui['live_mode']}"
            ),
        ]
    )


def events_table(events: list[dict]) -> str:
    headers = ["AT", "TYPE", "SEAT", "REQUEST", "AGENT", "DETAIL"]
    body = [
        [
            event.get("at"),
            event.get("event_type"),
            event.get("seat_id"),
            event.get("request_id"),
            event.get("agent_label"),
            event.get("detail"),
        ]
        for event in events
    ]
    return render_table(headers, body) if body else "(no events)"
