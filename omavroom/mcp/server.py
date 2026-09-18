"""FastMCP server exposing the omavroom seat manager to coding agents (Phase 5).

The server is a thin, stateless translation layer: every tool call becomes
one or more requests on the local daemon's Unix socket via
:class:`~omavroom.client.DaemonClient`. The daemon is the single process that
owns libvirt/QEMU; the MCP server never talks to it directly.

Nothing agent-facing blocks indefinitely
-----------------------------------------
- Fast reads (``pool_status``, ``seat_status``, ``exec_poll``, ...) return at
  once.
- Long lifecycle operations (``release_seat``, ``reset_seat``, ``prepare_repo``,
  ``export_seat``, ``retry_release``, ``force_discard``, ``reconcile``) return
  a ``job_id`` immediately; poll it with ``job_poll`` or wait (bounded) with
  ``job_wait``.
- ``request_seat`` returns a ``request_id`` immediately (it queues fairly);
  ``wait_for_seat`` is a *bounded* convenience poll.
- ``exec_run`` is the one synchronous convenience: it runs a command with a
  bounded default timeout and returns the combined output. For anything long,
  use ``exec_start`` + ``exec_poll`` (the async path) so the agent never
  blocks on a long-running command.

Concurrency model
-----------------
Fast calls share one daemon connection. Desktop ops
(``screenshot``/``input``/``peek_endpoint``) run on a dedicated short-lived
connection and the daemon bounds their wait for the per-seat lock, returning a
typed ``seat_busy`` error rather than blocking for the length of an
export/reset/release; because they do not share the connection they cannot
starve ``heartbeat`` or other tools.

Execution guidance
------------------
Use ``exec_run`` for short, bounded commands (a few seconds: ``git status``,
``ls``, a quick build check). Use ``exec_start``/``exec_poll``/``exec_kill``
for long commands (test suites, builds, servers): ``exec_start`` returns an
``exec_id`` right away, ``exec_poll`` streams a bounded ring buffer with
``stdout``/``stderr``/``exit_code``/``truncated``, and ``exec_kill``
terminates it.

Auto-start
----------
If the daemon socket is not live, the server starts ``python -m
omavroom.daemon`` detached (default ``libvirt`` provisioner) and waits for the
socket. Reuse an existing daemon otherwise. Disable with ``--no-autostart`` or
``OMAVROOM_MCP_AUTOSTART=0``. If the daemon cannot start, the server exits with
a clear error pointing at the daemon log.

Entry point: ``omavroom-mcp`` (stdio transport, as opencode launches it).
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import socket as _socket
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from omavroom.client import DaemonClient, DaemonClientError, DaemonNotRunning
from omavroom.daemon import (
    PROVISIONER_CHOICES,
    default_log_path,
    default_socket_path,
)
from omavroom.manager.execs import DEFAULT_LIST_OUTPUT_BUDGET_BYTES
from omavroom.manager.provisioner import InputEvent, RepoSpec

log = logging.getLogger("omavroom.mcp")

DEFAULT_START_TIMEOUT_S = 30.0
AUTOSTART_ENV = "OMAVROOM_MCP_AUTOSTART"
PROVISIONER_ENV = "OMAVROOM_PROVISIONER"
DEFAULT_PROVISIONER = "libvirt"
DEFAULT_EXEC_RUN_TIMEOUT_S = 30
DEFAULT_WAIT_TIMEOUT_S = 60.0
DEFAULT_JOB_TIMEOUT_S = 600.0
POLL_INTERVAL_S = 0.05
#: Engine timeout headroom for ``exec_run`` so its bounded client-side kill is
#: deterministic rather than racing the engine's own wall-clock timeout.
_EXEC_RUN_KILL_GRACE_S = 5

_TERMINAL_REQUEST_STATES = ("done", "cancelled", "expired", "failed")
_SETTLED_SEAT_STATES = ("ready", "busy", "held", "error")

DEFAULT_INSTRUCTIONS = (
    "Manage disposable Omarchy VM seats. Request a seat, run commands with "
    "exec_run (short) or exec_start/exec_poll (long), capture the desktop "
    "with screenshot, and release the seat when done. Long operations return "
    "a job_id to poll; exec_start returns an exec_id to poll."
)

#: Every tool registered on the FastMCP server, in a stable order.
MCP_TOOL_NAMES: tuple[str, ...] = (
    "pool_status",
    "queue_view",
    "seat_status",
    "list_seats",
    "list_events",
    "list_execs",
    "request_seat",
    "wait_for_seat",
    "heartbeat",
    "exec_start",
    "exec_poll",
    "exec_kill",
    "exec_run",
    "screenshot",
    "input",
    "peek_endpoint",
    "peek_url",
    "peek_attach",
    "prepare_repo",
    "export_seat",
    "release_seat",
    "reset_seat",
    "retry_release",
    "force_discard",
    "reconcile",
    "job_poll",
    "job_wait",
)


class MCPDaemonError(RuntimeError):
    """The MCP server could not reach or start the daemon."""


# --------------------------------------------------------------------------
# daemon lifecycle (reuse or start detached)
# --------------------------------------------------------------------------
def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def socket_is_live(path: str | Path, timeout: float = 0.5) -> bool:
    """Probe whether a Unix socket has a live listener."""
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def start_daemon_detached(
    *,
    socket_path: str | Path | None = None,
    provisioner: str | None = None,
) -> subprocess.Popen:
    """Start ``python -m omavroom.daemon`` in its own session.

    Detached (``start_new_session=True``) so the daemon outlives the MCP
    server process. The daemon configures its own logging (state dir); stdio
    is discarded so it can never scribble on the MCP stdio transport.
    """
    name = provisioner or os.environ.get(PROVISIONER_ENV) or DEFAULT_PROVISIONER
    argv = [sys.executable, "-m", "omavroom.daemon", "--provisioner", name]
    if socket_path is not None:
        # Keep an auto-started daemon on the exact socket the server will use.
        argv += ["--socket", str(socket_path)]
    env = dict(os.environ)
    env.setdefault(PROVISIONER_ENV, name)
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell, local binary
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


def ensure_daemon_client(
    socket_path: str | Path | None = None,
    *,
    autostart: bool | None = None,
    start_timeout: float = DEFAULT_START_TIMEOUT_S,
    provisioner: str | None = None,
) -> DaemonClient:
    """Reuse a live daemon, or start one detached and wait for the socket.

    Raises :class:`MCPDaemonError` with an actionable message when autostart
    is disabled, the daemon exits during startup, or the socket never becomes
    ready.
    """
    path = Path(socket_path) if socket_path else default_socket_path()
    client = DaemonClient(socket_path=path)
    if socket_is_live(path):
        return client
    if autostart is None:
        autostart = _env_flag(AUTOSTART_ENV, True)
    if not autostart:
        raise MCPDaemonError(f"omavroom daemon is not running at {path} and autostart is disabled")
    log.info("starting omavroom daemon detached (socket=%s)", path)
    proc = start_daemon_detached(socket_path=path, provisioner=provisioner)
    deadline = time.monotonic() + start_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise MCPDaemonError(
                f"omavroom daemon exited during startup (rc={proc.returncode}); "
                f"see {default_log_path()}"
            )
        if socket_is_live(path):
            try:
                client.ping()
                return client
            except DaemonClientError:
                pass
        time.sleep(POLL_INTERVAL_S)
    raise MCPDaemonError(
        f"omavroom daemon did not become ready within {start_timeout}s; see {default_log_path()}"
    )


# --------------------------------------------------------------------------
# tool implementations (plain methods -> directly unit-testable)
# --------------------------------------------------------------------------
class OmavroomTools:
    """The MCP toolset as methods over a :class:`DaemonClient`.

    Concurrency model
    -----------------
    Fast read/lifecycle calls share ``client``; the client serializes each
    round trip under a lock. Potentially-blocking *desktop* ops
    (``screenshot``/``input``/``peek_endpoint``) instead use a dedicated
    short-lived connection from ``client_factory``: a desktop op can wait on
    the daemon's per-seat lock (bounded, see ``seat_busy``), and that wait must
    not hold the shared client's lock and starve ``heartbeat`` or other tools.
    ``exec_run``/``job_wait``/``wait_for_seat`` poll with short calls, so they
    never hold the client lock for long.
    """

    def __init__(
        self,
        client: DaemonClient,
        *,
        client_factory: Callable[[], DaemonClient] | None = None,
    ) -> None:
        self.client = client
        self._client_factory = client_factory

    @contextmanager
    def _desktop_session(self):
        """Yield a client for a potentially-blocking desktop op.

        With no factory configured, falls back to the shared client (tests and
        single-caller use). The built-in factory in :func:`main` always yields
        a fresh connection so a blocked desktop op cannot starve others.
        """
        if self._client_factory is None:
            yield self.client
            return
        client = self._client_factory()
        try:
            yield client
        finally:
            client.close()

    # -- reads -----------------------------------------------------------
    def pool_status(self) -> dict:
        """What seats exist, what is busy, who is queued, and admission state."""
        return self.client.pool_status()

    def queue_view(self, include_history: bool = False) -> list:
        """The request queue (waiting + claimed; optionally history)."""
        return self.client.queue_view(include_history=include_history)

    def seat_status(self, request_id: int) -> dict:
        """Follow one request through queued -> provisioning -> ready."""
        return self.client.seat_status(request_id)

    def list_seats(self, include_history: bool = False) -> list:
        """List seats (optionally including ``off`` history rows)."""
        return self.client.list_seats(include_history=include_history)

    def list_events(self, limit: int = 200) -> list:
        """Recent scheduler/audit events (newest last)."""
        return self.client.list_events(limit=limit)

    def list_execs(
        self,
        seat_id: int,
        include_output: bool = True,
        max_total_output_bytes: int | None = DEFAULT_LIST_OUTPUT_BUDGET_BYTES,
    ) -> list:
        """Every exec recorded for a seat (most recent state included).

        Bounded by default: the aggregate stdout+stderr across the list is
        capped at ``max_total_output_bytes`` (default 8 KiB). Set
        ``include_output=False`` for metadata only, or
        ``max_total_output_bytes=None`` for the full per-record rings
        (use ``exec_poll`` for one exec's output).
        """
        return self.client.list_execs(
            seat_id,
            include_output=include_output,
            max_total_output_bytes=max_total_output_bytes,
        )

    # -- seats / leases --------------------------------------------------
    def request_seat(
        self,
        agent_label: str,
        seat_type: str,
        image: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Request a seat (queues fairly if full); returns a request_id.

        ``seat_type`` is ``"desktop"`` (graphical) or ``"terminal"``
        (headless). Follow with ``seat_status`` or the bounded ``wait_for_seat``.
        """
        pending = self.client.request_seat(agent_label, seat_type, image=image, project=project)
        view = pending.status()
        return {
            "request_id": pending.request_id,
            "status": view.get("status"),
            "position": view.get("position"),
            "seat": view.get("seat"),
        }

    def wait_for_seat(self, request_id: int, timeout_s: float = DEFAULT_WAIT_TIMEOUT_S) -> dict:
        """Bounded poll until the request is served or fails (never blocks forever)."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        view = self.client.seat_status(request_id)
        while time.monotonic() < deadline:
            if view.get("status") in _TERMINAL_REQUEST_STATES:
                break
            seat = view.get("seat")
            if seat is not None and seat.get("state") in _SETTLED_SEAT_STATES:
                break
            time.sleep(POLL_INTERVAL_S)
            view = self.client.seat_status(request_id)
        return view

    def heartbeat(
        self,
        seat_id: int | None = None,
        request_id: int | None = None,
        lease_id: int | None = None,
    ) -> dict:
        """Renew the lease on the independent channel (long execs stay live)."""
        return self.client.heartbeat(seat_id=seat_id, request_id=request_id, lease_id=lease_id)

    # -- exec ------------------------------------------------------------
    def exec_start(
        self,
        seat_id: int,
        command: str,
        label: str | None = None,
        timeout_s: int | None = None,
    ) -> dict:
        """Start a long command; returns an ``exec_id`` immediately.

        Output streams into a bounded ring buffer; poll with ``exec_poll`` and
        stop it with ``exec_kill``. Use ``exec_run`` instead for short commands.
        """
        exec_id = _new_exec_id()
        view = self.client.exec_start(
            seat_id, exec_id, command=command, label=label, timeout_s=timeout_s
        )
        result = dict(view)
        result["exec_id"] = exec_id
        return result

    def exec_poll(self, seat_id: int, exec_id: str) -> dict:
        """Poll an exec: ``state``, bounded ``stdout``/``stderr``, ``exit_code``."""
        return self.client.exec_poll(seat_id, exec_id)

    def exec_kill(self, seat_id: int, exec_id: str, signal: int = 9) -> dict:
        """Terminate a running exec and return its terminal view.

        ``signal`` only sets the recorded ``exit_code`` (``-signal``); it is
        **not** delivered to the guest process. The transport kills the local
        ``ssh`` process, closing the channel.
        """
        return self.client.exec_kill(seat_id, exec_id, signal=signal)

    def exec_run(
        self,
        seat_id: int,
        command: str,
        timeout_s: int = DEFAULT_EXEC_RUN_TIMEOUT_S,
        label: str | None = None,
    ) -> dict:
        """Run a command synchronously with a bounded timeout; return output.

        Convenience for short commands. If the command is still running at the
        deadline it is killed and ``timed_out`` is True with partial output.
        For long work use ``exec_start``/``exec_poll`` so nothing blocks.
        """
        if timeout_s is None or timeout_s < 1:
            raise ValueError("timeout_s must be >= 1")
        exec_id = _new_exec_id()
        # The engine gets a longer timeout than the agent-facing deadline, so
        # the bounded client-side kill is the deterministic path; the engine
        # timeout stays as a backstop if the kill is somehow lost.
        engine_timeout = timeout_s + _EXEC_RUN_KILL_GRACE_S
        self.client.exec_start(
            seat_id, exec_id, command=command, label=label, timeout_s=engine_timeout
        )
        deadline = time.monotonic() + timeout_s
        view = self.client.exec_poll(seat_id, exec_id)
        while view.get("state") == "running" and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)
            view = self.client.exec_poll(seat_id, exec_id)
        timed_out = view.get("state") == "running" or view.get("exit_code") == 124
        if view.get("state") == "running":
            try:
                view = self.client.exec_kill(seat_id, exec_id)
            except DaemonClientError:  # pragma: no cover - best-effort kill
                log.warning("failed to kill timed-out exec %s", exec_id, exc_info=True)
        result = dict(view)
        result["exec_id"] = exec_id
        result["timed_out"] = timed_out
        return result

    # -- desktop ops -----------------------------------------------------
    def screenshot(
        self, seat_id: int, max_width: int | None = None, max_bytes: int | None = None
    ) -> list:
        """Grab the guest framebuffer as a downscaled, byte-capped PNG.

        Returns MCP image content (so the agent can look at it) plus the same
        PNG as base64 text for callers that want the bytes.
        """
        with self._desktop_session() as client:
            data = client.screenshot(seat_id, max_width=max_width, max_bytes=max_bytes)
        return [Image(data=data, format="png"), base64.b64encode(data).decode("ascii")]

    def input(self, seat_id: int, events: list[dict]) -> dict:
        """Inject keystrokes/clicks inside a desktop seat.

        Each event is ``{"kind": "key"|"text"|"click", "value": "..."}``.
        """
        wire: list[InputEvent] = []
        for event in events:
            if isinstance(event, InputEvent):
                wire.append(event)
            elif isinstance(event, dict):
                wire.append(InputEvent(kind=event.get("kind"), value=event.get("value")))
            else:
                raise ValueError("each input event must be an object")
        with self._desktop_session() as client:
            return client.input(seat_id, wire)

    def peek_endpoint(self, seat_id: int) -> dict:
        """Resolve the on-demand viewer endpoint (never opens a window)."""
        with self._desktop_session() as client:
            return {"endpoint": client.peek_endpoint(seat_id)}

    def peek_url(self, seat_id: int) -> dict:
        """Alias of ``peek_endpoint``: the URL for an on-demand viewer."""
        return self.peek_endpoint(seat_id)

    def peek_attach(self, seat_id: int) -> dict:
        """Alias of ``peek_endpoint``: attaching is a client-side viewer action."""
        return self.peek_endpoint(seat_id)

    # -- repo / teardown (long ops: return a job_id) ---------------------
    def prepare_repo(self, seat_id: int, url: str, branch: str | None = None) -> dict:
        """Clone a credential-free repository into the seat (long op -> job_id)."""
        handle = self.client.prepare_repo(seat_id, RepoSpec(url=url, branch=branch))
        return {"job_id": handle.job_id}

    def export_seat(
        self,
        seat_id: int,
        repo: str,
        branch: str | None = None,
        ref: str | None = None,
    ) -> dict:
        """Export the seat's committed work (long op -> job_id; keeps the seat)."""
        handle = self.client.export_seat(seat_id, repo=repo, branch=branch, ref=ref)
        return {"job_id": handle.job_id}

    def release_seat(
        self,
        seat_id: int,
        repo: str | None = None,
        export: bool = True,
        branch: str | None = None,
        ref: str | None = None,
    ) -> dict:
        """Export (optional), then destroy the seat VM (long op -> job_id)."""
        handle = self.client.release_seat(seat_id, repo=repo, export=export, branch=branch, ref=ref)
        return {"job_id": handle.job_id}

    def reset_seat(self, seat_id: int) -> dict:
        """Revert a seat's overlay to the golden image (long op -> job_id)."""
        handle = self.client.reset_seat(seat_id)
        return {"job_id": handle.job_id}

    def retry_release(self, seat_id: int) -> dict:
        """Retry a stuck seat's persisted release/export (long op -> job_id)."""
        handle = self.client.retry_release(seat_id)
        return {"job_id": handle.job_id}

    def force_discard(self, seat_id: int, reason: str = "force_discard") -> dict:
        """Operator escape hatch: destroy a stuck seat without export (long op)."""
        handle = self.client.force_discard(seat_id, reason=reason)
        return {"job_id": handle.job_id}

    def reconcile(self) -> dict:
        """Adopt/reap provisioner VMs against durable state (long op -> job_id)."""
        handle = self.client.reconcile()
        return {"job_id": handle.job_id}

    # -- generic job polling ---------------------------------------------
    def job_poll(self, job_id: str) -> dict:
        """Poll any long-operation job (``pending``/``done``/``error``)."""
        return self.client.job_poll(job_id)

    def job_wait(self, job_id: str, timeout_s: float = DEFAULT_JOB_TIMEOUT_S) -> dict:
        """Bounded wait for a job; returns the last view (may still be pending)."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        view = self.client.job_poll(job_id)
        while view.get("state") == "pending" and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)
            view = self.client.job_poll(job_id)
        return view


def _new_exec_id() -> str:
    import uuid

    return f"mcp-{uuid.uuid4().hex[:16]}"


def _client_factory_from(client: DaemonClient) -> Callable[[], DaemonClient]:
    """Build same-parameters, fresh-connection clients for desktop ops."""

    def factory() -> DaemonClient:
        return DaemonClient(
            socket_path=client.socket_path,
            timeout=client.timeout,
            connect_retries=client.connect_retries,
            connect_retry_delay=client.connect_retry_delay,
        )

    return factory


# --------------------------------------------------------------------------
# server construction
# --------------------------------------------------------------------------
def build_server(
    tools: OmavroomTools,
    *,
    name: str = "omavroom",
    instructions: str | None = None,
) -> FastMCP:
    """Register every tool on a :class:`FastMCP` instance (stdio transport)."""
    mcp = FastMCP(name, instructions=instructions or DEFAULT_INSTRUCTIONS)
    for tool_name in MCP_TOOL_NAMES:
        mcp.add_tool(getattr(tools, tool_name))
    return mcp


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Console entry point: resolve/start the daemon, then serve MCP on stdio."""
    parser = argparse.ArgumentParser(
        prog="omavroom-mcp",
        description="MCP server for the omavroom disposable-VM seat manager.",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="daemon socket path (default: $XDG_RUNTIME_DIR/omavroom/daemon.sock)",
    )
    parser.add_argument(
        "--provisioner",
        choices=list(PROVISIONER_CHOICES),
        default=None,
        help="provisioner for an auto-started daemon (env: OMAVROOM_PROVISIONER)",
    )
    parser.add_argument(
        "--no-autostart",
        action="store_true",
        help="refuse to start a daemon; require one already running",
    )
    parser.add_argument(
        "--start-timeout",
        type=float,
        default=DEFAULT_START_TIMEOUT_S,
        help="seconds to wait for an auto-started daemon socket",
    )
    args = parser.parse_args(argv)

    # --no-autostart is decisive; otherwise defer to OMAVROOM_MCP_AUTOSTART
    # (None) so the env var is honored rather than silently overridden.
    autostart: bool | None = False if args.no_autostart else None
    try:
        client = ensure_daemon_client(
            args.socket,
            autostart=autostart,
            start_timeout=args.start_timeout,
            provisioner=args.provisioner,
        )
    except MCPDaemonError as exc:
        print(f"omavroom-mcp: {exc}", file=sys.stderr)
        return 1

    tools = OmavroomTools(client, client_factory=_client_factory_from(client))
    server = build_server(tools)
    try:
        server.run()
    except (DaemonNotRunning, DaemonClientError) as exc:  # pragma: no cover - server loop
        print(f"omavroom-mcp: daemon connection lost: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_EXEC_RUN_TIMEOUT_S",
    "DEFAULT_INSTRUCTIONS",
    "MCP_TOOL_NAMES",
    "MCPDaemonError",
    "OmavroomTools",
    "build_server",
    "ensure_daemon_client",
    "main",
    "socket_is_live",
    "start_daemon_detached",
]
