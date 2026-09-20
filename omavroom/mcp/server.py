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
(``screenshot``/``input``/``copy_*``/``launch_app``/``*_window``/``set_theme``/
``clipboard_*``/``peek_endpoint``) run on a dedicated short-lived connection and
the daemon bounds their wait for the per-seat lock, returning a typed
``seat_busy`` error rather than blocking for the length of an
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

Auto-heartbeat
--------------
While this server holds a seat it beats that seat's lease for the agent
(:class:`HeartbeatMonitor`): a background thread refreshes every tracked seat
at ``min(heartbeat_interval_s, heartbeat_timeout_s / 2)`` and each tool call
refreshes opportunistically. Agents therefore never have to babysit
``heartbeat``; a heartbeat timeout only detects a dead MCP server process. When
that happens the daemon puts the seat into **stasis** (``held``) rather than
destroying it.

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
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from omavroom.client import (
    DaemonClient,
    DaemonClientError,
    DaemonNotRunning,
    DaemonRequestError,
)
from omavroom.config import Config
from omavroom.daemon import (
    PROVISIONER_CHOICES,
    default_log_path,
    default_socket_path,
)
from omavroom.manager.execs import DEFAULT_LIST_OUTPUT_BUDGET_BYTES
from omavroom.manager.provisioner import InputEvent, RepoSpec
from omavroom.version import (
    MCP_CLIENT_CAPABILITIES,
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
)

log = logging.getLogger("omavroom.mcp")

DEFAULT_START_TIMEOUT_S = 30.0
AUTOSTART_ENV = "OMAVROOM_MCP_AUTOSTART"
PROVISIONER_ENV = "OMAVROOM_PROVISIONER"
DEFAULT_PROVISIONER = "libvirt"
DEFAULT_EXEC_RUN_TIMEOUT_S = 30
DEFAULT_WAIT_TIMEOUT_S = 60.0
DEFAULT_JOB_TIMEOUT_S = 600.0
POLL_INTERVAL_S = 0.05
#: Fallback auto-heartbeat cadence/budget for this MCP server, used only when
#: the caller does not pass the daemon's effective ``[leases]`` values.
DEFAULT_HEARTBEAT_INTERVAL_S = 60
DEFAULT_HEARTBEAT_TIMEOUT_S = 300
#: Engine timeout headroom for ``exec_run`` so its bounded client-side kill is
#: deterministic rather than racing the engine's own wall-clock timeout.
_EXEC_RUN_KILL_GRACE_S = 5

_TERMINAL_REQUEST_STATES = ("done", "cancelled", "expired", "failed")
_SETTLED_SEAT_STATES = ("ready", "busy", "held", "error")

DEFAULT_INSTRUCTIONS = (
    "Manage disposable Omarchy VM seats. Request a seat, run commands with "
    "exec_run (short) or exec_start/exec_poll (long), capture the native or "
    "cropped desktop with screenshot, and act like a user with input, "
    "launch_app, list_windows and the window/clipboard helpers. copy_in/out "
    "move files. Release the seat when done. Long operations return a job_id "
    "to poll; exec_start returns an exec_id to poll."
)

#: Short built-in how-to for project golden images. Returned by the ``guide``
#: tool (and the ``omavroom://guide`` resource) as a fallback for agents that
#: did not load the omavroom skill.
IMAGE_GUIDE = """\
Project golden images (agent flow)
1. Declare needs, do not guess packages: image_plan(project, tools=[...])
   maps high-level tools (rust, node, python, go, java, docker, tex, git,
   build, jq, ripgrep, fd) to Arch packages and shows the target image, the
   newly missing packages, and whether the current image already satisfies it.
2. Make it so: image_ensure(project, tools=[...], project_root=".")
   - status "satisfied" -> nothing to do.
   - status "needs_approval" -> show the recipe to the operator, then re-call
     with approved=True (or the operator enables [images] build_policy/allowlist).
   - status "building" -> poll job_poll/job_wait; the job result is "built".
   Passing project_root writes/updates .omavroom/image.toml so the recipe is
   versioned in the repo. The project is bound to the image on success.
3. Use it: request_seat(agent_label, seat_type, project=...).
4. Add a dependency later: call image_ensure again with the extra tool; only
   the new packages are applied to the existing image (delta build).
Inspect with image_list/image_status; image_logs(name) tails a build log.
`packages=[...]` installs literal packages; unknown safe tool names pass
through as packages. Auto-build policy lives in [images] (ask|allowlist|auto).
"""

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
    "copy_in",
    "copy_out",
    "launch_app",
    "list_windows",
    "focus_window",
    "resize_window",
    "move_window",
    "float_window",
    "set_theme",
    "clipboard_get",
    "clipboard_set",
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
    "image_list",
    "image_plan",
    "image_ensure",
    "image_status",
    "image_logs",
    "image_build",
    "image_rm",
    "guide",
    "job_poll",
    "job_wait",
)


class MCPDaemonError(RuntimeError):
    """The MCP server could not reach or start the daemon."""


class HeartbeatMonitor:
    """Best-effort heartbeat refresher for seats this MCP server is using.

    The MCP server is the agent's proxy, so the agent must not have to call
    ``heartbeat`` itself. Every tracked seat is beaten on a background daemon
    thread at a safe cadence -- ``min(interval, timeout/2)`` -- and
    opportunistically on each tool call. A heartbeat timeout then only detects
    a genuinely dead MCP server process (which stops beating), never a busy
    agent. Beats never raise into a tool call and never kill the thread.

    ``beat(seat_id)`` returns ``False`` when the seat no longer has an active
    lease (gone/held/off), which stops tracking; anything else keeps it.
    """

    #: Floor for the cadence so a pathological config cannot busy-loop.
    MIN_INTERVAL_S = 0.05

    def __init__(self, beat: Callable[[int], bool], *, interval_s: float, timeout_s: float) -> None:
        self._beat = beat
        self.interval_s = max(self.MIN_INTERVAL_S, min(float(interval_s), float(timeout_s) / 2.0))
        self._seats: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def track(self, seat_id: int) -> None:
        """Start (or keep) beating a seat."""
        with self._lock:
            self._seats.add(int(seat_id))

    def forget(self, seat_id: int) -> None:
        """Stop beating a seat (released, discarded, or no longer leased)."""
        with self._lock:
            self._seats.discard(int(seat_id))

    def tracked(self) -> set[int]:
        with self._lock:
            return set(self._seats)

    def refresh(self, seat_id: int | None = None) -> None:
        """Best-effort beat now (one seat, or every tracked seat); never raises."""
        targets = [seat_id] if seat_id is not None else sorted(self.tracked())
        for target in targets:
            self._beat_one(target)

    def _beat_one(self, seat_id: int) -> None:
        try:
            alive = self._beat(seat_id)
        except Exception:  # noqa: BLE001 - a heartbeat must never break a tool call
            log.debug("heartbeat refresh failed for seat %s", seat_id, exc_info=True)
            return
        if alive is False:
            self.forget(seat_id)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="omavroom-mcp-heartbeat", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.refresh()

    def stop(self) -> None:
        """Stop the background thread (idempotent; safe from any thread)."""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)


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
        heartbeat_interval_s: float | None = None,
        heartbeat_timeout_s: float | None = None,
        autostart_heartbeat: bool = True,
    ) -> None:
        self.client = client
        self._client_factory = client_factory
        self.heartbeats = HeartbeatMonitor(
            self._beat_seat,
            interval_s=(
                DEFAULT_HEARTBEAT_INTERVAL_S
                if heartbeat_interval_s is None
                else heartbeat_interval_s
            ),
            timeout_s=(
                DEFAULT_HEARTBEAT_TIMEOUT_S if heartbeat_timeout_s is None else heartbeat_timeout_s
            ),
        )
        if autostart_heartbeat:
            self.heartbeats.start()
        # Announce this client's version/capabilities once at startup, so the
        # daemon can flag an un-restarted opencode (missing ``auto_heartbeat``).
        # Best-effort: an older daemon that predates ``hello`` must not stop us.
        try:
            self.hello = client.hello()
        except DaemonClientError:
            log.debug("daemon does not support the hello handshake", exc_info=True)
            self.hello = {"client": MCP_CLIENT_NAME, "version": MCP_CLIENT_VERSION}

    # -- client identity / handshake -------------------------------------
    @property
    def client_name(self) -> str:
        return MCP_CLIENT_NAME

    @property
    def client_version(self) -> str:
        return MCP_CLIENT_VERSION

    @property
    def capabilities(self) -> tuple[str, ...]:
        return MCP_CLIENT_CAPABILITIES

    # -- auto-heartbeat --------------------------------------------------
    def _beat_seat(self, seat_id: int) -> bool:
        """One best-effort beat; ``False`` means the seat is no longer leased.

        ``not_found`` (no active lease: released, held, or off) stops
        tracking. Any other failure (daemon briefly unavailable, timeout) is
        transient and keeps the seat tracked.
        """
        try:
            self.client.heartbeat(seat_id=seat_id)
        except DaemonRequestError as exc:
            return exc.code != "not_found"
        except DaemonClientError:
            return True
        return True

    def _use_seat(self, seat_id: int) -> None:
        """Track a seat and refresh it opportunistically (best-effort)."""
        self.heartbeats.track(seat_id)
        self.heartbeats.refresh(seat_id)

    def stop(self) -> None:
        """Stop the auto-heartbeat thread (call on server shutdown)."""
        self.heartbeats.stop()

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

    def ping(self) -> dict:
        """Liveness + this client's version/capabilities and the server's."""
        reply = self.client.ping_info()
        reply["client"] = MCP_CLIENT_NAME
        reply["client_version"] = MCP_CLIENT_VERSION
        reply["capabilities"] = list(MCP_CLIENT_CAPABILITIES)
        reply.setdefault("server", SERVER_NAME)
        reply.setdefault("server_version", SERVER_VERSION)
        return reply

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
        self._use_seat(seat_id)
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
        (headless). A ``project`` label binds the request to that project's
        configured image (``[projects.<name>] image=...``) when one is set;
        an explicit ``image`` always wins. The resolved image appears in the
        returned seat view. Follow with ``seat_status`` or the bounded
        ``wait_for_seat``.
        """
        pending = self.client.request_seat(agent_label, seat_type, image=image, project=project)
        view = pending.status()
        seat = view.get("seat")
        if isinstance(seat, dict) and seat.get("id") is not None:
            self._use_seat(int(seat["id"]))
        return {
            "request_id": pending.request_id,
            "status": view.get("status"),
            "position": view.get("position"),
            "seat": seat,
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
        seat = view.get("seat")
        if isinstance(seat, dict) and seat.get("id") is not None:
            self._use_seat(int(seat["id"]))
        return view

    def heartbeat(
        self,
        seat_id: int | None = None,
        request_id: int | None = None,
        lease_id: int | None = None,
    ) -> dict:
        """Renew the lease on the independent channel (long execs stay live)."""
        lease = self.client.heartbeat(seat_id=seat_id, request_id=request_id, lease_id=lease_id)
        if isinstance(lease, dict) and lease.get("seat_id") is not None:
            self.heartbeats.track(int(lease["seat_id"]))
        return lease

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
        self._use_seat(seat_id)
        exec_id = _new_exec_id()
        view = self.client.exec_start(
            seat_id, exec_id, command=command, label=label, timeout_s=timeout_s
        )
        result = dict(view)
        result["exec_id"] = exec_id
        return result

    def exec_poll(self, seat_id: int, exec_id: str) -> dict:
        """Poll an exec: ``state``, bounded ``stdout``/``stderr``, ``exit_code``."""
        self._use_seat(seat_id)
        return self.client.exec_poll(seat_id, exec_id)

    def exec_kill(self, seat_id: int, exec_id: str, signal: int = 9) -> dict:
        """Terminate a running exec and return its terminal view.

        ``signal`` only sets the recorded ``exit_code`` (``-signal``); it is
        **not** delivered to the guest process. The transport kills the local
        ``ssh`` process, closing the channel.
        """
        self._use_seat(seat_id)
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
        self._use_seat(seat_id)
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
        self,
        seat_id: int,
        max_width: int | None = None,
        max_bytes: int | None = None,
        region: list[int] | None = None,
    ) -> list:
        """Grab the guest framebuffer as an MCP image (and base64 text).

        ``max_width=None`` (or ``0``) returns the **native, full-resolution**
        frame; pass a positive width to downscale. ``region=[x, y, w, h]``
        crops the native frame to that rectangle before encoding. The encoded
        PNG is still byte-capped, so a raw frame is never a multi-MB reply.
        """
        self._use_seat(seat_id)
        requested = 0 if max_width is None else max_width
        crop = tuple(region) if region is not None else None
        with self._desktop_session() as client:
            data = client.screenshot(seat_id, max_width=requested, max_bytes=max_bytes, region=crop)
        return [Image(data=data, format="png"), base64.b64encode(data).decode("ascii")]

    def input(self, seat_id: int, events: list[dict]) -> dict:
        """Inject keystrokes/clicks inside a desktop seat.

        Each event is ``{"kind": "key"|"text"|"type"|"click", "value": "..."}``.
        ``key`` is a single keysym or ``Mod+...+Key`` combo, ``text`` types in
        bulk, ``type`` types per character (add ``"delay_ms": N`` to set the
        delay between characters and exercise incremental rendering), and
        ``click`` takes ``"x,y[,button]"``.
        """
        wire: list[InputEvent] = []
        for event in events:
            if isinstance(event, InputEvent):
                wire.append(event)
            elif isinstance(event, dict):
                wire.append(
                    InputEvent(
                        kind=event.get("kind"),
                        value=event.get("value"),
                        delay_ms=event.get("delay_ms"),
                    )
                )
            else:
                raise ValueError("each input event must be an object")
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return client.input(seat_id, wire)

    def copy_in(self, seat_id: int, host_path: str, guest_path: str) -> dict:
        """Copy a host file into the seat over the pinned SSH key.

        The host path is constrained to the daemon's transfer root (under the
        omavroom data dir); a path outside it is rejected. ``guest_path`` must
        be absolute.
        """
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return client.copy_in(seat_id, host_path, guest_path)

    def copy_out(self, seat_id: int, guest_path: str, host_path: str) -> dict:
        """Copy a seat file out to the host over the pinned SSH key.

        The host destination is constrained to the daemon's transfer root
        (under the omavroom data dir); a path outside it is rejected.
        ``guest_path`` must be absolute.
        """
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return client.copy_out(seat_id, guest_path, host_path)

    def launch_app(self, seat_id: int, command: str, tui: bool = False) -> dict:
        """Launch an app on the seat's desktop.

        ``tui=True`` runs ``command`` in a terminal via ``omarchy-launch-tui``
        (for TUIs); otherwise it is dispatched directly through Hyprland.
        """
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return client.launch_app(seat_id, command, tui=tui)

    def list_windows(self, seat_id: int) -> dict:
        """List the seat desktop's windows (class, title, address, geometry).

        Use the returned ``class``/``title`` with ``focus_window`` (or pass a
        regex directly).
        """
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return {"windows": client.list_windows(seat_id)}

    def focus_window(self, seat_id: int, match: str) -> dict:
        """Focus the window whose class/title matches ``match`` (regex)."""
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return {"window": client.focus_window(seat_id, match)}

    def resize_window(self, seat_id: int, match: str, width: int, height: int) -> dict:
        """Resize the matching window to ``width`` x ``height`` pixels."""
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return client.resize_window(seat_id, match, width, height)

    def move_window(self, seat_id: int, match: str, x: int, y: int) -> dict:
        """Move the matching window's top-left corner to ``(x, y)``."""
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return client.move_window(seat_id, match, x, y)

    def float_window(self, seat_id: int, match: str, on: bool = True) -> dict:
        """Turn floating on/off for the matching window."""
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return client.float_window(seat_id, match, on)

    def set_theme(self, seat_id: int, name: str) -> dict:
        """Apply the named Omarchy theme (``omarchy theme set``)."""
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return client.set_theme(seat_id, name)

    def clipboard_get(self, seat_id: int) -> dict:
        """Read the seat's Wayland clipboard text (``wl-paste``)."""
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return {"text": client.clipboard_get(seat_id)}

    def clipboard_set(self, seat_id: int, text: str) -> dict:
        """Set the seat's Wayland clipboard text (``wl-copy``)."""
        self._use_seat(seat_id)
        with self._desktop_session() as client:
            return client.clipboard_set(seat_id, text)

    def peek_endpoint(self, seat_id: int) -> dict:
        """Resolve the on-demand viewer endpoint (never opens a window)."""
        self._use_seat(seat_id)
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
        self._use_seat(seat_id)
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
        self._use_seat(seat_id)
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
        self._use_seat(seat_id)
        handle = self.client.release_seat(seat_id, repo=repo, export=export, branch=branch, ref=ref)
        return {"job_id": handle.job_id}

    def reset_seat(self, seat_id: int) -> dict:
        """Revert a seat's overlay to the golden image (long op -> job_id)."""
        self._use_seat(seat_id)
        handle = self.client.reset_seat(seat_id)
        return {"job_id": handle.job_id}

    def retry_release(self, seat_id: int) -> dict:
        """Retry a stuck seat's persisted release/export (long op -> job_id)."""
        self._use_seat(seat_id)
        handle = self.client.retry_release(seat_id)
        return {"job_id": handle.job_id}

    def force_discard(self, seat_id: int, reason: str = "force_discard") -> dict:
        """Operator escape hatch: destroy a stuck seat without export (long op)."""
        self._use_seat(seat_id)
        handle = self.client.force_discard(seat_id, reason=reason)
        self.heartbeats.forget(seat_id)
        return {"job_id": handle.job_id}

    def reconcile(self) -> dict:
        """Adopt/reap provisioner VMs against durable state (long op -> job_id)."""
        handle = self.client.reconcile()
        return {"job_id": handle.job_id}

    # -- project images --------------------------------------------------
    def image_list(self) -> list:
        """Registered images: name, golden path, seat type, project bindings."""
        return self.client.image_list()

    def image_plan(
        self,
        project: str,
        tools: list[str] | None = None,
        packages: list[str] | None = None,
        base: str | None = None,
    ) -> dict:
        """Read-only plan for a project image: recipe, missing packages, satisfied.

        Resolves high-level ``tools`` (``rust``, ``node``, ``python``, ``go``,
        ``java``, ``docker``, ``tex``, ``git``, ``build``, ``jq``, ``ripgrep``,
        ``fd``) to Arch packages, merges them with the project image's recorded
        package set, and reports the target image name, the newly missing
        packages, and whether the current image already satisfies the request.
        Never builds or writes anything.
        """
        return self.client.image_plan(project, tools=tools, packages=packages, base=base)

    def image_ensure(
        self,
        project: str,
        tools: list[str] | None = None,
        packages: list[str] | None = None,
        base: str | None = None,
        project_root: str | None = None,
        approved: bool = False,
    ) -> dict:
        """Idempotently make a project image satisfy tools/packages.

        Returns ``status="satisfied"`` (already current), ``status=
        "needs_approval"`` with the recipe to confirm, or ``status="building"``
        with a ``job_id``. ``project_root`` versions the recipe into
        ``.omavroom/image.toml``; ``approved=True`` forces a build after the
        operator approves.
        """
        return self.client.image_ensure(
            project,
            tools=tools,
            packages=packages,
            base=base,
            project_root=project_root,
            approved=approved,
        )

    def image_status(self, name: str) -> dict:
        """Live state for one image build (``pending``/``running``/``done``/``error``)."""
        return self.client.image_status(name)

    def image_logs(self, name: str, tail: int = 50) -> dict:
        """The last ``tail`` lines of an image build's log."""
        return self.client.image_logs(name, tail=tail)

    def guide(self) -> str:
        """How to prepare and use a project golden image (short built-in flow)."""
        return IMAGE_GUIDE

    def image_build(
        self,
        name: str,
        recipe: str | None = None,
        base: str | None = None,
        packages: list[str] | None = None,
        post: list[str] | None = None,
        approved: bool = False,
    ) -> dict:
        """Build (or delta-upgrade) a project image (long op -> job_id).

        Never auto-builds: ``approved=True`` must be passed explicitly, which
        the agent should obtain from the operator after showing the packages
        that will be installed. Without it this raises and nothing is built.
        """
        if approved is not True:
            raise ValueError(
                "image_build requires approved=True: confirm the package "
                "install with the operator before building"
            )
        handle = self.client.image_build(
            name, recipe=recipe, base=base, packages=packages, post=post, approved=True
        )
        return {"job_id": handle.job_id}

    def image_rm(self, name: str) -> dict:
        """Unregister an image (and delete it when it lives in the store)."""
        return self.client.image_rm(name)

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

    @mcp.resource(
        "omavroom://guide",
        name="omavroom guide",
        description="How to prepare and use a project golden image.",
        mime_type="text/plain",
    )
    def _guide_resource() -> str:
        return IMAGE_GUIDE

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

    # Use the same lease budget the daemon enforces; fall back to the built-in
    # defaults if the local config cannot be read.
    try:
        leases = Config.load().leases
    except (OSError, ValueError):
        leases = None
    tools = OmavroomTools(
        client,
        client_factory=_client_factory_from(client),
        heartbeat_interval_s=leases.heartbeat_interval_s if leases else None,
        heartbeat_timeout_s=leases.heartbeat_timeout_s if leases else None,
    )
    server = build_server(tools)
    try:
        server.run()
    except (DaemonNotRunning, DaemonClientError) as exc:  # pragma: no cover - server loop
        print(f"omavroom-mcp: daemon connection lost: {exc}", file=sys.stderr)
        return 1
    finally:
        tools.stop()
        client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_EXEC_RUN_TIMEOUT_S",
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "DEFAULT_HEARTBEAT_TIMEOUT_S",
    "DEFAULT_INSTRUCTIONS",
    "IMAGE_GUIDE",
    "MCP_CLIENT_CAPABILITIES",
    "MCP_CLIENT_NAME",
    "MCP_CLIENT_VERSION",
    "MCP_TOOL_NAMES",
    "MCPDaemonError",
    "HeartbeatMonitor",
    "OmavroomTools",
    "build_server",
    "ensure_daemon_client",
    "main",
    "socket_is_live",
    "start_daemon_detached",
]
