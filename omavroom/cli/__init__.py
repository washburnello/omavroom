"""Command-line interface for omavroom.

The CLI is a `cli/` subpackage (rather than a single module) as a growth
seam for Phase 6, which adds the full operator CLI plus the metadata-only
Textual TUI companion alongside it.

Phase 0 provides working argument parsing with stub subcommands so
`omavroom --help` works and each subcommand name is reserved. Real
implementations land in Phase 6 (driving the Phase 4 manager daemon);
the native GUI Command Center comes later.
"""

from __future__ import annotations

import argparse
import sys

STUB_COMMANDS: tuple[str, ...] = ("status", "request", "exec", "screenshot", "release")

_COMMAND_HELP: dict[str, str] = {
    "status": "show pool/seat/queue state (stub; real view in Phase 6)",
    "request": "request a seat, queueing if full (stub; scheduler in Phase 4)",
    "exec": "run a command inside a seat (stub; async exec in Phase 4/5)",
    "screenshot": "grab a seat's framebuffer as PNG (stub; Phase 5)",
    "release": "verify work export, then destroy the seat VM (stub; Phase 4)",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the `omavroom` argument parser with all stub subcommands."""
    parser = argparse.ArgumentParser(
        prog="omavroom",
        description="Disposable-VM seat manager for coding agents.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name in STUB_COMMANDS:
        subparsers.add_parser(name, help=_COMMAND_HELP[name])
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `omavroom` console script. Returns exit code."""
    args = build_parser().parse_args(argv)
    if args.command is None:
        build_parser().print_help()
        return 0
    # Stderr + exit 2 (argparse convention): a stub must never look like
    # success to a future proof script checking exit codes.
    print(
        f"omavroom {args.command}: not yet implemented (Phase 0 skeleton).",
        file=sys.stderr,
    )
    return 2
