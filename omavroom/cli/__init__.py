"""Command-line interface for omavroom.

Phase 4B2 lands the daemon entry point, makes ``status`` a thin client of the
running daemon, and adds the two operator escape hatches for stuck seats
(``retry-release`` / ``force-discard``). The richer operator CLI and the
metadata-only Textual TUI are Phase 6; the remaining subcommands stay
reserved stubs until then.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

STUB_COMMANDS: tuple[str, ...] = ("request", "exec", "screenshot", "release")

_COMMAND_HELP: dict[str, str] = {
    "daemon": "run the seat manager daemon in the foreground",
    "status": "show pool/seat/queue state from the running daemon",
    "retry-release": "retry a stuck seat's persisted release/export intent",
    "force-discard": "destroy a stuck seat's VM to unblock the pool",
    "request": "request a seat, queueing if full (stub; Phase 6 CLI)",
    "exec": "run a command inside a seat (stub; Phase 5/6)",
    "screenshot": "grab a seat's framebuffer as PNG (stub; Phase 6)",
    "release": "verify work export, then destroy the seat VM (stub; Phase 6)",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the `omavroom` argument parser with all subcommands."""
    from omavroom.daemon import PROVISIONER_CHOICES

    parser = argparse.ArgumentParser(
        prog="omavroom",
        description="Disposable-VM seat manager for coding agents.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    daemon = subparsers.add_parser("daemon", help=_COMMAND_HELP["daemon"])
    daemon.add_argument(
        "--provisioner",
        choices=list(PROVISIONER_CHOICES),
        default=os.environ.get("OMAVROOM_PROVISIONER", "libvirt"),
        help="provisioner backend (env: OMAVROOM_PROVISIONER)",
    )
    subparsers.add_parser("status", help=_COMMAND_HELP["status"])
    retry = subparsers.add_parser("retry-release", help=_COMMAND_HELP["retry-release"])
    retry.add_argument("seat_id", type=int)
    discard = subparsers.add_parser("force-discard", help=_COMMAND_HELP["force-discard"])
    discard.add_argument("seat_id", type=int)
    for name in STUB_COMMANDS:
        subparsers.add_parser(name, help=_COMMAND_HELP[name])
    return parser


def _status() -> int:
    from omavroom.client import DaemonClient, DaemonClientError

    client = DaemonClient()
    try:
        status = client.pool_status()
    except DaemonClientError as exc:
        print(f"omavroom status: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def _operator_action(command: str, seat_id: int) -> int:
    """Run a daemon job action (retry-release / force-discard) and print it."""
    from omavroom.client import DaemonClient, DaemonClientError

    client = DaemonClient()
    try:
        if command == "retry-release":
            job = client.retry_release(seat_id)
        else:
            job = client.force_discard(seat_id)
        result = job.result(timeout=600)
    except DaemonClientError as exc:
        print(f"omavroom {command}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `omavroom` console script. Returns exit code."""
    args = build_parser().parse_args(argv)
    if args.command is None:
        build_parser().print_help()
        return 0
    if args.command == "daemon":
        from omavroom.daemon import run_daemon

        return run_daemon(provisioner=args.provisioner)
    if args.command == "status":
        return _status()
    if args.command in ("retry-release", "force-discard"):
        return _operator_action(args.command, args.seat_id)
    # Stderr + exit 2 (argparse convention): a stub must never look like
    # success to a future proof script checking exit codes.
    print(
        f"omavroom {args.command}: not yet implemented (Phase 6 CLI).",
        file=sys.stderr,
    )
    return 2
