"""Command-line interface for omavroom (Phase 4B2 stub -> full Phase 6 CLI).

Every operator command is a thin :class:`~omavroom.client.DaemonClient`
client: argument parsing and presentation live here, all state and all VM
work live behind the daemon's frozen protocol-v1 socket. ``status``,
``seats``, ``queue`` and ``events`` read; ``request``/``release``/``reset``/
``destroy``/``force-discard``/``admission`` change lifecycle state;
``screenshot``/``peek`` touch the framebuffer/viewer only for a desktop seat;
``settings``/``config show`` and ``image list`` read the *local* config and
need no daemon. Nothing here ever talks to libvirt directly.

Exit codes
----------
- ``0`` success
- ``1`` daemon/request failure (including "daemon is not running")
- ``2`` usage error (argparse), or a not-yet-implemented command
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from omavroom.config import SEAT_TYPES
from omavroom.poolview import (
    build_seat_rows,
    build_waiter_rows,
    format_mb,
    headroom_mb,
    pool_totals,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

DEFAULT_REFRESH_INTERVAL_S = 2.0
DEFAULT_JOB_TIMEOUT_S = 600.0

#: Where a ``screenshot`` without ``-o`` lands, kept out of the CWD/repo.
DEFAULT_SCREENSHOT_SUBDIR = Path(".local") / "share" / "omavroom" / "screenshots"

#: Commands reserved for later phases; they exit 2 so a proof script can
#: never mistake an unimplemented command for success.
STUB_COMMANDS: tuple[str, ...] = ("exec",)


def default_screenshot_path(name: str) -> Path:
    """Default screenshot path under ``~/.local/share/omavroom/screenshots``.

    The directory is created on demand. ``$HOME`` is resolved at call time
    (not import time) so an override -- and tests -- behave as expected.
    """
    directory = Path.home() / DEFAULT_SCREENSHOT_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return directory / f"omavroom-{name}-{stamp}.png"


_COMMAND_HELP: dict[str, str] = {
    "init": "scaffold a project for isolated work (write .omavroom/image.toml)",
    "daemon": "run the seat manager daemon in the foreground",
    "status": "show a live pool/seat/queue table from the running daemon",
    "seats": "list live seats",
    "queue": "list queued (waiting) seat requests with positions",
    "request": "request a seat, queueing if the pool is full",
    "release": "verify work export, then destroy the seat VM",
    "reset": "revert a seat's overlay to the golden image without releasing",
    "retry-release": "retry a stuck seat's persisted release/export intent",
    "force-discard": "destroy a stuck seat's VM to unblock the pool",
    "destroy": "destroy a seat's VM immediately, without exporting work",
    "screenshot": "grab a desktop seat's framebuffer as a PNG file",
    "peek": "print a seat's on-demand viewer endpoint (never auto-opens)",
    "events": "show the daemon's recent event log",
    "admission": "show or set the runtime admission override",
    "image": "list, build (from a project recipe) or remove golden images",
    "settings": ("show the effective configuration, or 'settings set <section>.<key> <value>'"),
    "config": "configuration helpers (alias of 'settings')",
    "tui": "open the metadata-only Textual monitor wall (over SSH friendly)",
    "gui": "open the native Qt6/QML Command Center (monitor wall + queue)",
    "exec": "run a command inside a seat (stub; later phase)",
}


class CliError(Exception):
    """A client-side argument/state problem the daemon did not report."""


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    """Build the ``omavroom`` argument parser with all subcommands."""
    from omavroom.daemon import PROVISIONER_CHOICES

    parser = argparse.ArgumentParser(
        prog="omavroom",
        description="Disposable-VM seat manager for coding agents.",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="override the daemon Unix socket path (env: XDG_RUNTIME_DIR layout)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init = subparsers.add_parser("init", help=_COMMAND_HELP["init"])
    init.add_argument(
        "path",
        nargs="?",
        default=None,
        help="project directory (default: the current directory)",
    )
    init.add_argument(
        "--tools",
        action="append",
        default=None,
        metavar="TOOLS",
        help="extra tools to add to the detected set (comma-separated; repeatable)",
    )
    init.add_argument("--base", default=None, help="base image name (default: golden-omarchy)")
    init.add_argument("--force", action="store_true", help="overwrite an existing recipe")
    _add_json(init)

    daemon = subparsers.add_parser("daemon", help=_COMMAND_HELP["daemon"])
    daemon.add_argument(
        "--provisioner",
        choices=list(PROVISIONER_CHOICES),
        default=os.environ.get("OMAVROOM_PROVISIONER", "libvirt"),
        help="provisioner backend (env: OMAVROOM_PROVISIONER)",
    )
    daemon.add_argument("--db", default=None, help="override the state database path")
    daemon.add_argument("--log", default=None, help="override the daemon log path")

    status = subparsers.add_parser("status", help=_COMMAND_HELP["status"])
    status.add_argument("--watch", action="store_true", help="refresh in place until stopped")
    status.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_REFRESH_INTERVAL_S,
        help="seconds between --watch refreshes (default: %(default)s)",
    )
    status.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="stop after N refreshes (0 = until interrupted; for scripting)",
    )
    _add_json(status)

    seats = subparsers.add_parser("seats", help=_COMMAND_HELP["seats"])
    _add_json(seats)

    queue = subparsers.add_parser("queue", help=_COMMAND_HELP["queue"])
    _add_json(queue)

    request = subparsers.add_parser("request", help=_COMMAND_HELP["request"])
    request.add_argument("seat_type", choices=list(SEAT_TYPES))
    request.add_argument("--agent", required=True, help="agent label (required)")
    request.add_argument("--project", default=None, help="repo/project label")
    request.add_argument("--image", default=None, help="pin a named golden image")
    request.add_argument("--wait", action="store_true", help="block until the seat is ready")
    request.add_argument(
        "--timeout", type=float, default=DEFAULT_JOB_TIMEOUT_S, help="wait timeout in seconds"
    )
    _add_json(request)

    for name, help_text in (
        ("release", _COMMAND_HELP["release"]),
        ("reset", _COMMAND_HELP["reset"]),
        ("retry-release", _COMMAND_HELP["retry-release"]),
        ("force-discard", _COMMAND_HELP["force-discard"]),
        ("destroy", _COMMAND_HELP["destroy"]),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("seat", help="seat id or seat name")
        sub.add_argument(
            "--timeout", type=float, default=DEFAULT_JOB_TIMEOUT_S, help="job timeout in seconds"
        )
        _add_json(sub)
        if name == "release":
            sub.add_argument("--repo", default=None, help="host checkout to export into")
            sub.add_argument("--branch", default=None, help="task branch to export")
            sub.add_argument("--ref", default=None, help="explicit ref to push")
            sub.add_argument(
                "--no-export", action="store_true", help="destroy without exporting work"
            )
        if name in ("force-discard", "destroy"):
            default_reason = "destroy" if name == "destroy" else "force_discard"
            sub.add_argument("--reason", default=default_reason, help="audit reason")

    screenshot = subparsers.add_parser("screenshot", help=_COMMAND_HELP["screenshot"])
    screenshot.add_argument("seat", help="seat id or seat name")
    screenshot.add_argument(
        "-o",
        "--out",
        default=None,
        help=(
            "output PNG path, or '-' for raw bytes"
            " (default: ~/.local/share/omavroom/screenshots/omavroom-<seat>-<ts>.png)"
        ),
    )
    screenshot.add_argument("--max-width", type=int, default=None, help="downscale width cap")
    _add_json(screenshot)

    peek = subparsers.add_parser("peek", help=_COMMAND_HELP["peek"])
    peek.add_argument("seat", help="seat id or seat name")
    peek.add_argument(
        "--viewer",
        default=None,
        metavar="CMD",
        help="explicitly launch this viewer with the endpoint (e.g. 'vncviewer' or 'cmd {}')",
    )
    _add_json(peek)

    events = subparsers.add_parser("events", help=_COMMAND_HELP["events"])
    events.add_argument("--limit", type=int, default=200, help="max events (default: %(default)s)")
    _add_json(events)

    admission = subparsers.add_parser("admission", help=_COMMAND_HELP["admission"])
    admission.add_argument(
        "--override",
        choices=["auto", "allow", "deny"],
        default=None,
        help="set the runtime admission override (auto|allow|deny)",
    )
    admission.add_argument(
        "--clear-prewarm",
        action="store_true",
        help="clear a suspended prewarm floor (optionally for --seat-type)",
    )
    admission.add_argument("--seat-type", choices=list(SEAT_TYPES), default=None)
    _add_json(admission)

    image = subparsers.add_parser("image", help=_COMMAND_HELP["image"])
    image_sub = image.add_subparsers(dest="image_command", metavar="<action>")
    image_list = image_sub.add_parser("list", help="list configured golden images")
    _add_json(image_list)
    image_build = image_sub.add_parser(
        "build", help="build a project image from .omavroom/image.toml (asks first)"
    )
    image_build.add_argument("name", help="name to register the built image under")
    image_build.add_argument(
        "--recipe",
        default=None,
        help="recipe path (default: .omavroom/image.toml in the current directory)",
    )
    image_build.add_argument("--base", default=None, help="override the recipe's base image")
    image_build.add_argument(
        "--yes", "-y", action="store_true", help="skip the install confirmation prompt"
    )
    image_build.add_argument(
        "--timeout", type=float, default=DEFAULT_JOB_TIMEOUT_S, help="job timeout in seconds"
    )
    _add_json(image_build)
    image_rm = image_sub.add_parser("rm", help="unregister (and delete) a configured image")
    image_rm.add_argument("name")
    image_rm.add_argument(
        "--yes", "-y", action="store_true", help="skip the removal confirmation prompt"
    )
    _add_json(image_rm)

    settings = subparsers.add_parser("settings", help=_COMMAND_HELP["settings"])
    settings_sub = settings.add_subparsers(dest="settings_command", metavar="<action>")
    settings_show = settings_sub.add_parser(
        "show", help="show the effective (merged) configuration (no daemon)"
    )
    _add_json(settings_show)
    settings_set = settings_sub.add_parser(
        "set", help="validate and persist a config value through the daemon"
    )
    settings_set.add_argument("assignment", metavar="<section>.<key>", help="e.g. golden.profile")
    settings_set.add_argument("value", help="new value (JSON scalar, else a string)")
    _add_json(settings_set)
    _add_json(settings)

    config = subparsers.add_parser("config", help=_COMMAND_HELP["config"])
    config_sub = config.add_subparsers(dest="config_command", metavar="<action>")
    config_show = config_sub.add_parser("show", help="show the effective configuration")
    _add_json(config_show)

    tui = subparsers.add_parser("tui", help=_COMMAND_HELP["tui"])
    tui.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_REFRESH_INTERVAL_S,
        help="auto-refresh seconds (default: %(default)s)",
    )

    gui = subparsers.add_parser("gui", help=_COMMAND_HELP["gui"])
    gui.add_argument(
        "--interval",
        type=float,
        default=None,
        help="override [gui].wall_interval_s (screenshot/status polling seconds)",
    )
    gui.add_argument(
        "--screenshot-width",
        type=int,
        default=None,
        help="override [gui].thumbnail_width (wall-scale desktop capture width)",
    )
    gui.add_argument(
        "--viewer",
        default=os.environ.get("OMAVROOM_VIEWER"),
        help="viewer command for click-to-peek ({}=endpoint); never auto-launched",
    )

    for name in STUB_COMMANDS:
        subparsers.add_parser(name, help=_COMMAND_HELP[name])
    return parser


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------
def _client(args: argparse.Namespace):
    from omavroom.client import DaemonClient

    return DaemonClient(getattr(args, "socket", None))


def _dump(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _fail(command: str, exc: BaseException) -> int:
    print(f"omavroom {command}: {exc}", file=sys.stderr)
    return EXIT_ERROR


def _run_job(command: str, job, *, timeout: float | None) -> tuple[object | None, int]:
    """Wait for a daemon job handle; return ``(result, exit_code)``."""
    from omavroom.client import DaemonClientError

    try:
        result = job.result(timeout=timeout or None)
    except DaemonClientError as exc:
        return None, _fail(command, exc)
    return result, EXIT_OK


def _resolve_seat(client, token: str) -> tuple[int, str]:
    """Resolve a seat id or name to ``(seat_id, name)`` with a clear error."""
    text = str(token).strip()
    seats = client.list_seats()
    if text.isdigit():
        seat_id = int(text)
        for seat in seats:
            if int(seat.get("id", -1)) == seat_id:
                return seat_id, str(seat.get("name") or seat_id)
        raise CliError(f"no such live seat: {seat_id}")
    for seat in seats:
        if str(seat.get("name")) == text:
            return int(seat["id"]), text
    raise CliError(f"no seat named {text!r}")


# --------------------------------------------------------------------------
# command handlers
# --------------------------------------------------------------------------
def _status(args: argparse.Namespace) -> int:
    from omavroom.cli.format import status_report
    from omavroom.client import DaemonClientError

    client = _client(args)
    if args.watch:
        return _watch(client, args)
    try:
        status = client.pool_status()
    except DaemonClientError as exc:
        return _fail("status", exc)
    finally:
        client.close()
    if args.json:
        _dump(status)
    else:
        print(status_report(status))
    return EXIT_OK


def _watch(client, args: argparse.Namespace) -> int:
    """Refresh the status table in place until interrupted or N iterations.

    Every poll is printed, up or down. The returned exit code reflects the
    *last* poll only: ``0`` if the final refresh succeeded, ``1`` if the daemon
    was still down (so a scripted ``--watch --iterations N`` sees the same
    failure signal as the one-shot ``status``).
    """
    from omavroom.cli.format import status_report
    from omavroom.client import DaemonClientError

    count = 0
    last_failed = False
    try:
        while True:
            try:
                status = client.pool_status()
                payload = (
                    json.dumps(status, indent=2, sort_keys=True, default=str)
                    if args.json
                    else status_report(status)
                )
                last_failed = False
            except DaemonClientError as exc:
                payload = (
                    json.dumps({"ok": False, "error": str(exc)}, sort_keys=True)
                    if args.json
                    else f"daemon not running: {exc}"
                )
                last_failed = True
            if sys.stdout.isatty():
                sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()
            count += 1
            if args.iterations and count >= args.iterations:
                break
            time.sleep(max(0.0, args.interval))
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
    return EXIT_ERROR if last_failed else EXIT_OK


def _seats(args: argparse.Namespace) -> int:
    from omavroom.cli.format import seats_table
    from omavroom.client import DaemonClientError

    client = _client(args)
    try:
        status = client.pool_status()
    except DaemonClientError as exc:
        return _fail("seats", exc)
    finally:
        client.close()
    if args.json:
        _dump(status.get("seats") or [])
    else:
        rows = build_seat_rows(status)
        print(seats_table(rows) if rows else "(none)")
    return EXIT_OK


def _queue(args: argparse.Namespace) -> int:
    from omavroom.cli.format import queue_table
    from omavroom.client import DaemonClientError

    client = _client(args)
    try:
        status = client.pool_status()
    except DaemonClientError as exc:
        return _fail("queue", exc)
    finally:
        client.close()
    waiters = build_waiter_rows(status)
    if args.json:
        waiting = [r for r in status.get("queue") or [] if r.get("status") == "waiting"]
        _dump(waiting)
    else:
        print(queue_table(waiters) if waiters else "(empty)")
    return EXIT_OK


def _request(args: argparse.Namespace) -> int:
    from omavroom.client import DaemonClientError

    client = _client(args)
    try:
        pending = client.request_seat(
            args.agent, args.seat_type, image=args.image, project=args.project
        )
        if not args.wait:
            view = {"request_id": pending.request_id}
            if args.json:
                _dump(view)
            else:
                print(pending.request_id)
            return EXIT_OK
        view = pending.wait_ready(timeout=args.timeout or None)
    except DaemonClientError as exc:
        return _fail("request", exc)
    finally:
        client.close()
    if args.json:
        _dump(view)
    else:
        seat = view.get("seat") or {}
        print(
            f"request {view.get('id')}: {view.get('status')}"
            f" seat={seat.get('name')} state={seat.get('state')}"
        )
    if view.get("status") in ("failed", "expired", "cancelled"):
        return EXIT_ERROR
    return EXIT_OK


def _release(args: argparse.Namespace) -> int:
    from omavroom.client import DaemonClientError

    client = _client(args)
    try:
        seat_id, _ = _resolve_seat(client, args.seat)
        job = client.release_seat(
            seat_id,
            repo=args.repo,
            export=not args.no_export,
            branch=args.branch,
            ref=args.ref,
        )
        result, code = _run_job("release", job, timeout=args.timeout)
    except (DaemonClientError, CliError) as exc:
        return _fail("release", exc)
    finally:
        client.close()
    if result is None:
        return code
    if args.json:
        _dump(result)
    else:
        message = result.get("message") or ""
        print(
            f"release seat {seat_id}: destroyed={result.get('destroyed')}"
            f" held={result.get('held')}{f' ({message})' if message else ''}"
        )
    return EXIT_ERROR if result.get("held") else EXIT_OK


def _seat_job(command: str):
    def handler(args: argparse.Namespace) -> int:
        from omavroom.client import DaemonClientError

        client = _client(args)
        try:
            seat_id, _ = _resolve_seat(client, args.seat)
            if command == "reset":
                job = client.reset_seat(seat_id)
            elif command == "retry-release":
                job = client.retry_release(seat_id)
            else:
                job = client.force_discard(seat_id, reason=getattr(args, "reason", "force_discard"))
            result, code = _run_job(command, job, timeout=args.timeout)
        except (DaemonClientError, CliError) as exc:
            return _fail(command, exc)
        finally:
            client.close()
        if result is None:
            return code
        if args.json:
            _dump(result)
        else:
            state = result.get("state") if isinstance(result, dict) else result
            print(f"{command} seat {seat_id}: state={state}")
        return EXIT_OK

    return handler


def _screenshot(args: argparse.Namespace) -> int:
    from omavroom.client import DaemonClientError

    client = _client(args)
    try:
        seat_id, name = _resolve_seat(client, args.seat)
        data = client.screenshot(seat_id, max_width=args.max_width)
    except DaemonClientError as exc:
        return _fail("screenshot", exc)
    except CliError as exc:
        return _fail("screenshot", exc)
    finally:
        client.close()
    if args.out == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return EXIT_OK
    try:
        path = Path(args.out) if args.out else default_screenshot_path(name)
        path.write_bytes(data)
    except OSError as exc:
        target = args.out or str(Path.home() / DEFAULT_SCREENSHOT_SUBDIR)
        return _fail("screenshot", CliError(f"cannot write screenshot to {target}: {exc}"))
    if args.json:
        _dump({"seat_id": seat_id, "name": name, "path": str(path), "bytes": len(data)})
    else:
        print(path)
    return EXIT_OK


def _peek(args: argparse.Namespace) -> int:
    from omavroom.client import DaemonClientError

    client = _client(args)
    try:
        seat_id, _ = _resolve_seat(client, args.seat)
        endpoint = client.peek_endpoint(seat_id)
    except (DaemonClientError, CliError) as exc:
        return _fail("peek", exc)
    finally:
        client.close()
    if args.json:
        _dump({"seat_id": seat_id, "endpoint": endpoint})
    else:
        print(endpoint)
    if not args.viewer:
        return EXIT_OK
    cmd = shlex.split(args.viewer)
    if not cmd:
        return _fail("peek", CliError("--viewer command is empty"))
    if any("{}" in part for part in cmd):
        cmd = [part.replace("{}", endpoint) for part in cmd]
    else:
        cmd.append(endpoint)
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        return _fail("peek", CliError(f"cannot launch viewer {cmd[0]!r}: {exc}"))


def _events(args: argparse.Namespace) -> int:
    from omavroom.cli.format import events_table
    from omavroom.client import DaemonClientError

    client = _client(args)
    try:
        events = client.list_events(limit=args.limit)
    except DaemonClientError as exc:
        return _fail("events", exc)
    finally:
        client.close()
    if args.json:
        _dump(events)
    else:
        print(events_table(events))
    return EXIT_OK


def _admission(args: argparse.Namespace) -> int:
    from omavroom.client import DaemonClientError

    client = _client(args)
    try:
        if args.override:
            result = client.set_admission_override(args.override)
            if args.json:
                _dump(result)
            else:
                print(f"admission override set to {result.get('override', args.override)}")
            return EXIT_OK
        if args.clear_prewarm:
            result = client.clear_prewarm_backoff(args.seat_type)
            if args.json:
                _dump(result)
            else:
                scope = args.seat_type or "all seat types"
                print(f"cleared prewarm backoff for {scope}")
            return EXIT_OK
        status = client.pool_status()
    except DaemonClientError as exc:
        return _fail("admission", exc)
    finally:
        client.close()
    if args.json:
        _dump(
            {
                "admission_override": status.get("admission_override"),
                "per_type": status.get("per_type"),
            }
        )
    else:
        print(
            f"admission override: {status.get('admission_override')}"
            f" | auto-admit budget {format_mb(headroom_mb(status))}"
        )
        print(f"seats: {pool_totals(status.get('per_type') or {})}")
        for seat_type, info in (status.get("per_type") or {}).items():
            decision = "admit" if info.get("admitted") else "hold"
            print(f"  {seat_type}: {decision} ({info.get('reason')})")
    return EXIT_OK


def _image_list(args: argparse.Namespace) -> int:
    from omavroom.cli.format import render_table
    from omavroom.config import Config

    try:
        config = Config.load()
    except (OSError, ValueError) as exc:
        return _fail("image list", exc)
    entries = [
        {
            "name": name,
            "seat_type": image.seat_type,
            "golden": str(image.path),
            "exists": image.path.exists(),
            "projects": sorted(
                project for project, cfg in config.projects.items() if cfg.image == name
            ),
        }
        for name, image in config.images.items()
    ]
    if args.json:
        _dump(entries)
        return EXIT_OK
    rows = [
        [
            entry["name"],
            entry["seat_type"] or "-",
            entry["golden"],
            "yes" if entry["exists"] else "no",
            ",".join(entry["projects"]) or "-",
        ]
        for entry in entries
    ]
    print(render_table(["NAME", "SEAT_TYPE", "GOLDEN", "EXISTS", "PROJECTS"], rows))
    if config.projects:
        project_rows = [[project, cfg.image] for project, cfg in sorted(config.projects.items())]
        print()
        print(render_table(["PROJECT", "IMAGE"], project_rows))
    return EXIT_OK


def _resolve_image_build(args: argparse.Namespace):
    """Resolve the recipe locally so the CLI can show and confirm the build.

    Returns ``(base, packages, post)`` or raises :class:`CliError`. Doing this
    client-side means the daemon's working directory never decides which
    recipe applies.
    """
    from omavroom.config import Config, ImageRecipe, default_recipe_path, load_recipe

    try:
        path = Path(args.recipe) if args.recipe else default_recipe_path()
        if path.exists():
            recipe = load_recipe(path)
        elif args.recipe:
            raise CliError(f"recipe not found: {path}")
        elif args.base:
            recipe = ImageRecipe(base=args.base)
        else:
            raise CliError(
                f"no recipe at {path}: pass --recipe PATH or --base IMAGE, "
                "or add .omavroom/image.toml"
            )
        config = Config.load()
    except (OSError, ValueError) as exc:
        raise CliError(str(exc)) from exc
    base = args.base or recipe.base or config.image_for("desktop")
    return base, list(recipe.packages), list(recipe.post)


def _image_build(args: argparse.Namespace) -> int:
    from omavroom.client import DaemonClientError

    try:
        base, packages, post = _resolve_image_build(args)
    except CliError as exc:
        return _fail("image build", exc)
    if not args.yes:
        print(f"image build {args.name!r}: base={base}")
        print(f"  packages: {', '.join(packages) if packages else '(none)'}")
        if post:
            print(f"  post: {'; '.join(post)}")
        answer = input("Install these into a scratch VM and build the image? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return EXIT_OK
    client = _client(args)
    try:
        job = client.image_build(args.name, base=base, packages=packages, post=post, approved=True)
        result, code = _run_job("image build", job, timeout=args.timeout)
    except DaemonClientError as exc:
        return _fail("image build", exc)
    finally:
        client.close()
    if result is None:
        return code
    if args.json:
        _dump(result)
    else:
        print(
            f"image {result.get('name')}: base={result.get('base')}"
            f" delta={str(result.get('delta')).lower()} path={result.get('path')}"
        )
    return EXIT_OK


def _image_rm(args: argparse.Namespace) -> int:
    from omavroom.client import DaemonClientError

    if not args.yes:
        answer = input(f"Remove image {args.name!r}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return EXIT_OK
    client = _client(args)
    try:
        result = client.image_rm(args.name)
    except DaemonClientError as exc:
        return _fail("image rm", exc)
    finally:
        client.close()
    if args.json:
        _dump(result)
    else:
        print(f"removed image {result.get('name')} (deleted={result.get('deleted')})")
    return EXIT_OK


def _parse_tool_flags(values: list[str] | None) -> list[str]:
    """Flatten repeated/comma-separated ``--tools`` values into a tool list."""
    tools: list[str] = []
    for value in values or []:
        for token in str(value).split(","):
            token = token.strip()
            if token and token not in tools:
                tools.append(token)
    return tools


def _print_init_summary(result: dict) -> None:
    detected = ", ".join(result["detected_tools"]) or "(none)"
    print(f"init {result['path']}: detected {detected}")
    print(f"  tools: {', '.join(result['tools']) or '(none)'}")
    print(f"  packages: {', '.join(result['packages']) or '(none)'}")
    if result["recipe_written"]:
        print(f"  recipe: {result['recipe_path']} (written, base={result['base']})")
    else:
        print(f"  recipe: {result['recipe_path']} (exists; use --force to overwrite)")
    if result["agents_created"]:
        print("  AGENTS.md: created with the isolated-work section")
    elif result["agents_changed"]:
        print("  AGENTS.md: appended the isolated-work section")
    else:
        print("  AGENTS.md: already documents isolated work")
    print(f"  image: {result['image']}")
    print(f"  next: image_ensure(project={result['project']!r}, project_root={result['path']!r})")


def _init(args: argparse.Namespace) -> int:
    """Scaffold a project for isolated work; never talks to the daemon."""
    from omavroom.scaffold import init_project

    try:
        result = init_project(
            args.path or ".",
            tools=_parse_tool_flags(args.tools),
            base=args.base,
            force=args.force,
        )
    except (OSError, ValueError) as exc:
        return _fail("init", exc)
    if args.json:
        _dump(result)
    else:
        _print_init_summary(result)
    return EXIT_OK


def _settings(args: argparse.Namespace) -> int:
    """Print the effective (merged) local config; never talks to the daemon."""
    from omavroom.cli.format import settings_dict, settings_report
    from omavroom.config import Config

    try:
        config = Config.load()
    except (OSError, ValueError) as exc:
        return _fail("settings", exc)
    if args.json:
        _dump(settings_dict(config))
    else:
        print(settings_report(config))
    return EXIT_OK


def _parse_set_value(text: str) -> object:
    """Interpret a CLI value as JSON when possible, else as a plain string."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _settings_set(args: argparse.Namespace) -> int:
    """Persist one ``section.key=value`` through the daemon.

    The daemon is the single writer: it validates against the config schema,
    updates ``~/.config/omavroom/config.toml`` preserving other keys, and
    returns the applied value. A bad value surfaces as a daemon ``invalid``
    error (exit 1).
    """
    from omavroom.client import DaemonClientError

    section, separator, key = args.assignment.partition(".")
    if not separator or not section or not key:
        print(
            "omavroom settings set: expected <section>.<key> (e.g. golden.profile)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    value = _parse_set_value(args.value)
    client = _client(args)
    try:
        result = client.set_config_value(section, key, value)
    except DaemonClientError as exc:
        return _fail("settings set", exc)
    finally:
        client.close()
    if args.json:
        _dump(result)
    else:
        print(f"{result.get('section')}.{result.get('key')} = {result.get('value')!r}")
    return EXIT_OK


def _tui(args: argparse.Namespace) -> int:
    try:
        from omavroom.tui import run_tui
    except ImportError as exc:  # pragma: no cover - textual is a hard dependency
        return _fail("tui", CliError(f"Textual is not installed: {exc}"))
    return run_tui(socket_path=getattr(args, "socket", None), refresh_interval=args.interval)


def _gui(args: argparse.Namespace) -> int:
    """Launch the native Command Center; PySide6 is imported lazily."""
    from omavroom.config import Config

    try:
        from omavroom.gui.app import run_gui
    except ImportError as exc:  # pragma: no cover - pyside6 is a hard dependency
        return _fail("gui", CliError(f"PySide6 is not installed: {exc}"))
    try:
        config = Config.load()
    except (OSError, ValueError) as exc:
        return _fail("gui", exc)
    try:
        return run_gui(
            socket_path=getattr(args, "socket", None),
            interval=args.interval,
            screenshot_width=args.screenshot_width,
            viewer=args.viewer,
            config=config,
        )
    except SystemExit as exc:  # PySide6 missing: run_gui raises SystemExit
        return _fail("gui", CliError(str(exc)))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``omavroom`` console script. Returns exit code."""
    args = build_parser().parse_args(argv)
    if args.command is None:
        build_parser().print_help()
        return EXIT_OK
    if args.command == "daemon":
        from omavroom.daemon import run_daemon

        return run_daemon(
            provisioner=args.provisioner,
            db_path=args.db,
            socket_path=args.socket,
            log_path=args.log,
        )
    if args.command in STUB_COMMANDS:
        print(
            f"omavroom {args.command}: not yet implemented (later phase).",
            file=sys.stderr,
        )
        return EXIT_USAGE
    handlers = {
        "init": _init,
        "status": _status,
        "seats": _seats,
        "queue": _queue,
        "request": _request,
        "release": _release,
        "reset": _seat_job("reset"),
        "retry-release": _seat_job("retry-release"),
        "force-discard": _seat_job("force-discard"),
        "destroy": _seat_job("destroy"),
        "screenshot": _screenshot,
        "peek": _peek,
        "events": _events,
        "admission": _admission,
        "tui": _tui,
        "gui": _gui,
    }
    if args.command == "image":
        action = getattr(args, "image_command", None)
        if action == "list":
            return _image_list(args)
        if action == "build":
            return _image_build(args)
        if action == "rm":
            return _image_rm(args)
        print("omavroom image: choose an action (try 'omavroom image list')", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "config":
        if getattr(args, "config_command", None) == "show":
            return _settings(args)
        print("omavroom config: choose an action (try 'omavroom config show')", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "settings":
        if getattr(args, "settings_command", None) == "set":
            return _settings_set(args)
        # Bare ``settings`` and ``settings show`` both print the effective config.
        return _settings(args)
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover - parser only yields known commands
        print(f"omavroom {args.command}: unknown command", file=sys.stderr)
        return EXIT_USAGE
    return handler(args)


__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_USAGE", "STUB_COMMANDS", "build_parser", "main"]
