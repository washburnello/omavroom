"""Typed Python client for the omavroom daemon socket (Phase 4B2).

This is the transport Phase 5's MCP server and Phase 6's CLI/TUI build on:
one method per wire method, typed errors, and job handles for the daemon's
long operations. It is deliberately dependency-free (stdlib + the client
module only) so any host-side component can import it.

Protocol recap
--------------
Newline-delimited JSON over a Unix-domain socket::

    -> {"id": 1, "method": "pool_status", "params": {}}
    <- {"id": 1, "ok": true, "result": {...}}

Long operations return ``{"job_id": ...}``; poll with
:meth:`DaemonClient.job_poll` (or the :class:`JobHandle` helpers).

Errors
------
- :class:`DaemonNotRunning` — no daemon is listening (after connection
  retries). This is the "start the daemon first" error.
- :class:`DaemonRequestError` — the daemon returned a structured error;
  carries ``code`` (``not_found``, ``invalid``, ``provisioner_error``, ...)
  and ``message``.
- :class:`DaemonAlreadyRunning` — the daemon reported ``already_running``
  (another daemon owns the socket).
- :class:`DaemonTimeout` — a request or job did not finish before the
  deadline (a raw socket ``TimeoutError`` is converted to this).

The client verifies the response ``id`` echoes the request ``id`` before
accepting a reply. A dropped/restarted daemon is retried once per
:meth:`call` for the connection-level failures that are safe to retry
(broken pipe / reset); structured errors are never retried.

Frozen Phase 5 mapping: ``peek_url`` and ``peek_attach`` are aliases of
:meth:`DaemonClient.peek_endpoint` (attaching is a client-side viewer
action); ``exec_start`` carries the command/timeout and ``exec_poll``
returns bounded ``stdout``/``stderr``/``exit_code``/``truncated``.
"""

from __future__ import annotations

import itertools
import json
import socket
import threading
import time
from pathlib import Path

from omavroom.daemon import default_socket_path
from omavroom.manager.execs import DEFAULT_LIST_OUTPUT_BUDGET_BYTES
from omavroom.manager.provisioner import InputEvent, RepoSpec

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_CONNECT_RETRIES = 3
DEFAULT_CONNECT_RETRY_DELAY_S = 0.1


class DaemonClientError(Exception):
    """Base class for client-side failures."""


class DaemonNotRunning(DaemonClientError):
    """The daemon socket is absent or not accepting connections."""


class DaemonRequestError(DaemonClientError):
    """The daemon answered with a structured error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class DaemonAlreadyRunning(DaemonRequestError):
    """The daemon reported that another daemon already owns the socket."""

    def __init__(self, message: str = "daemon already running") -> None:
        super().__init__("already_running", message)


class DaemonTimeout(DaemonClientError):
    """A request or job did not complete before the caller's deadline."""


class JobHandle:
    """A daemon-side long operation; poll or wait for its result."""

    def __init__(self, client: DaemonClient, job_id: str, method: str | None = None) -> None:
        self.client = client
        self.job_id = job_id
        self.method = method

    def poll(self) -> dict:
        """Return the current job view (``state`` is pending/done/error)."""
        return self.client.call("job_poll", job_id=self.job_id)

    def wait(self, timeout: float | None = None, *, interval: float = 0.05) -> dict:
        """Block until the job finishes; return its final view or raise."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            view = self.poll()
            if view.get("state") != "pending":
                return view
            if deadline is not None and time.monotonic() >= deadline:
                raise DaemonTimeout(f"job {self.job_id} did not finish within {timeout}s")
            time.sleep(interval)

    def result(self, timeout: float | None = None) -> object:
        """Wait for the job and return its result (raises on job error)."""
        view = self.wait(timeout)
        if view.get("state") == "error":
            error = view.get("error") or {}
            raise DaemonRequestError(
                error.get("code", "job_error"), error.get("message", "job failed")
            )
        return view.get("result")


_TERMINAL_REQUEST_STATES = ("done", "cancelled", "expired", "failed")
_SEAT_SETTLED_STATES = ("ready", "busy", "held", "error")


class PendingRequest:
    """A daemon seat request; poll with :meth:`status` or wait with
    :meth:`wait_ready` (mirrors ``RequestHandle``)."""

    def __init__(self, client: DaemonClient, request_id: int) -> None:
        self.client = client
        self.request_id = request_id

    def status(self) -> dict:
        """Non-blocking request/seat view (mirrors ``RequestHandle.status``)."""
        return self.client.seat_status(self.request_id)

    def wait_ready(self, timeout: float | None = None, *, interval: float = 0.05) -> dict:
        """Block until the request is served or fails; return the current view.

        Mirrors ``RequestHandle.wait_ready``: a timeout returns the last view
        rather than raising.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            view = self.status()
            if view.get("status") in _TERMINAL_REQUEST_STATES:
                return view
            seat = view.get("seat")
            if seat is not None and seat.get("state") in _SEAT_SETTLED_STATES:
                return view
            if deadline is not None and time.monotonic() >= deadline:
                return view
            time.sleep(interval)


class DaemonClient:
    """Thread-safe client for the omavroom daemon's Unix socket."""

    def __init__(
        self,
        socket_path: str | Path | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        connect_retries: int = DEFAULT_CONNECT_RETRIES,
        connect_retry_delay: float = DEFAULT_CONNECT_RETRY_DELAY_S,
    ) -> None:
        self.socket_path = Path(socket_path) if socket_path else default_socket_path()
        self.timeout = timeout
        self.connect_retries = connect_retries
        self.connect_retry_delay = connect_retry_delay
        self._sock: socket.socket | None = None
        self._stream = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    # -- connection plumbing --------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def shutdown(self) -> None:
        """Force-close the socket *without* waiting for the request lock.

        :meth:`close` acquires ``self._lock``, which :meth:`call` holds for the
        whole (possibly blocking) request/response cycle. A caller on another
        thread that needs to unblock an in-flight request -- the GUI's
        background poll worker on application shutdown -- must not wait on
        that lock. This closes the underlying socket directly, which makes a
        blocked ``readline`` return promptly; the request then fails with the
        usual connection error and the connection is discarded on the next
        call. Thread-safe: the lock-free read of the socket reference is
        idempotent and tolerant of a concurrent :meth:`call` teardown.
        """
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _close_locked(self) -> None:
        stream = self._stream
        self._stream = None
        sock = self._sock
        self._sock = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _connect_locked(self) -> None:
        last: OSError | None = None
        for attempt in range(self.connect_retries + 1):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect(str(self.socket_path))
            except OSError as exc:
                last = exc
                sock.close()
                if attempt < self.connect_retries:
                    time.sleep(self.connect_retry_delay)
                continue
            self._sock = sock
            self._stream = sock.makefile("rwb")
            return
        raise DaemonNotRunning(
            f"omavroom daemon is not running at {self.socket_path}: {last}"
        ) from last

    def call(self, method: str, **params) -> object:
        """Send one request and return its result, or raise a typed error."""
        with self._lock:
            for attempt in range(2):
                try:
                    if self._sock is None:
                        self._connect_locked()
                    return self._call_locked(method, params)
                except (BrokenPipeError, ConnectionResetError) as exc:
                    self._close_locked()
                    if attempt == 1:
                        raise DaemonNotRunning(
                            f"omavroom daemon connection lost at {self.socket_path}: {exc}"
                        ) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    def _call_locked(self, method: str, params: dict) -> object:
        request_id = next(self._ids)
        payload = json.dumps({"id": request_id, "method": method, "params": params})
        assert self._stream is not None
        try:
            self._stream.write(payload.encode("utf-8") + b"\n")
            self._stream.flush()
            line = self._stream.readline()
        except TimeoutError as exc:
            self._close_locked()
            raise DaemonTimeout(
                f"daemon request {method!r} timed out after {self.timeout}s"
            ) from exc
        except OSError:
            self._close_locked()
            raise
        if not line:
            self._close_locked()
            raise DaemonNotRunning(f"omavroom daemon closed the connection at {self.socket_path}")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            self._close_locked()
            raise DaemonRequestError("bad_response", f"invalid JSON from daemon: {exc}") from exc
        if not isinstance(response, dict):
            self._close_locked()
            raise DaemonRequestError("bad_response", "daemon response is not a JSON object")
        if response.get("id") != request_id:
            # Never accept a response that is not the one we asked for.
            self._close_locked()
            raise DaemonRequestError(
                "bad_response",
                f"response id {response.get('id')!r} != request id {request_id!r}",
            )
        if not response.get("ok"):
            error = response.get("error") or {}
            code = error.get("code", "error")
            message = error.get("message", "request failed")
            if code == "already_running":
                raise DaemonAlreadyRunning(message)
            raise DaemonRequestError(code, message)
        return response.get("result")

    # -- fast calls ------------------------------------------------------
    def ping(self) -> bool:
        return bool(self.call("ping").get("pong"))

    def ping_info(self) -> dict:
        """Full ``ping`` reply, including the ``protocol`` version."""
        return self.call("ping")

    def protocol_version(self) -> int:
        """The daemon's wire protocol version (see ``daemon.PROTOCOL_VERSION``)."""
        return int(self.call("ping").get("protocol", 0))

    def hello(
        self,
        client: str | None = None,
        *,
        version: str | None = None,
        capabilities: list[str] | None = None,
    ) -> dict:
        """Announce this client's version/capabilities (startup handshake).

        Defaults to the shared MCP client identity in :mod:`omavroom.version`.
        Returns the server's ``{server, server_version, protocol, capabilities}``.
        """
        from omavroom.version import (
            MCP_CLIENT_CAPABILITIES,
            MCP_CLIENT_NAME,
            MCP_CLIENT_VERSION,
        )

        return self.call(
            "hello",
            client=client or MCP_CLIENT_NAME,
            version=version or MCP_CLIENT_VERSION,
            capabilities=list(capabilities)
            if capabilities is not None
            else list(MCP_CLIENT_CAPABILITIES),
        )

    def pool_status(self) -> dict:
        return self.call("pool_status")

    def queue_view(self, *, include_history: bool = False) -> list:
        return self.call("queue_view", include_history=include_history)

    def seat_status(self, request_id: int) -> dict:
        return self.call("seat_status", request_id=request_id)

    def list_seats(self, *, include_history: bool = False) -> list:
        return self.call("list_seats", include_history=include_history)

    def list_events(self, *, limit: int | None = 200) -> list:
        return self.call("list_events", limit=limit)

    def list_execs(
        self,
        seat_id: int,
        *,
        include_output: bool = True,
        max_total_output_bytes: int | None = DEFAULT_LIST_OUTPUT_BUDGET_BYTES,
    ) -> list:
        """Bounded exec list; ``include_output=False`` for metadata only.

        Pass ``max_total_output_bytes=None`` for the full per-record rings.
        """
        return self.call(
            "list_execs",
            seat_id=seat_id,
            include_output=include_output,
            max_total_output_bytes=max_total_output_bytes,
        )

    def heartbeat(
        self,
        *,
        seat_id: int | None = None,
        request_id: int | None = None,
        lease_id: int | None = None,
    ) -> dict:
        return self.call("heartbeat", seat_id=seat_id, request_id=request_id, lease_id=lease_id)

    def begin_work(self, seat_id: int) -> dict:
        return self.call("begin_work", seat_id=seat_id)

    def finish_work(self, seat_id: int) -> dict:
        return self.call("finish_work", seat_id=seat_id)

    def exec_start(
        self,
        seat_id: int,
        exec_id: str,
        *,
        label: str | None = None,
        command: str | None = None,
        timeout_s: int | None = None,
    ) -> dict:
        return self.call(
            "exec_start",
            seat_id=seat_id,
            exec_id=exec_id,
            label=label,
            command=command,
            timeout_s=timeout_s,
        )

    def exec_poll(self, seat_id: int, exec_id: str) -> dict:
        return self.call("exec_poll", seat_id=seat_id, exec_id=exec_id)

    def exec_output(
        self, seat_id: int, exec_id: str, *, stdout: str = "", stderr: str = ""
    ) -> dict:
        """Stream output into a running exec's bounded buffers (Phase 5)."""
        return self.call(
            "exec_output", seat_id=seat_id, exec_id=exec_id, stdout=stdout, stderr=stderr
        )

    def exec_finish(
        self,
        seat_id: int,
        exec_id: str,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> dict:
        return self.call(
            "exec_finish",
            seat_id=seat_id,
            exec_id=exec_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    def exec_kill(self, seat_id: int, exec_id: str, *, signal: int = 9) -> dict:
        return self.call("exec_kill", seat_id=seat_id, exec_id=exec_id, signal=signal)

    def screenshot(
        self,
        seat_id: int,
        *,
        max_width: int | None = None,
        max_bytes: int | None = None,
        region: tuple[int, int, int, int] | list[int] | None = None,
    ) -> bytes:
        """Fetch one framebuffer as raw PNG bytes.

        ``max_width=0`` requests the native, full-resolution frame (no
        downscale); omitting ``max_width`` keeps the daemon's downscale
        default so existing callers are unchanged. A positive width is a
        bounded downscale. ``region=(x, y, w, h)`` crops the native frame
        before encoding.

        Any malformed daemon reply (missing/wrong-typed ``png_base64`` or
        invalid base64) is normalized into a :class:`DaemonRequestError` so a
        single bad response can never raise ``KeyError``/``TypeError``/
        ``binascii.Error`` into a caller's QTimer slot.
        """
        import base64
        import binascii

        params: dict = {"seat_id": seat_id}
        if max_width is not None:
            params["max_width"] = max_width
        if max_bytes is not None:
            params["max_bytes"] = max_bytes
        if region is not None:
            params["region"] = list(region)
        result = self.call("screenshot", **params)
        if not isinstance(result, dict):
            raise DaemonRequestError("bad_response", "screenshot response is not a JSON object")
        encoded = result.get("png_base64")
        if not isinstance(encoded, str):
            raise DaemonRequestError(
                "bad_response", "screenshot response is missing a string png_base64"
            )
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DaemonRequestError(
                "bad_response", f"invalid base64 in screenshot response: {exc}"
            ) from exc

    def input(self, seat_id: int, events: list[InputEvent] | list[dict]) -> dict:
        wire = [
            (
                {"kind": event.kind, "value": event.value, "delay_ms": event.delay_ms}
                if isinstance(event, InputEvent)
                else event
            )
            for event in events
        ]
        return self.call("input", seat_id=seat_id, events=wire)

    def peek_endpoint(self, seat_id: int) -> str:
        """Resolve the on-demand viewer endpoint (``vnc://...``)."""
        return self.call("peek_endpoint", seat_id=seat_id)["endpoint"]

    def peek_url(self, seat_id: int) -> str:
        """MCP ``peek_url`` alias: the endpoint for an on-demand viewer."""
        return self.peek_endpoint(seat_id)

    def peek_attach(self, seat_id: int) -> str:
        """MCP ``peek_attach`` alias: attaching is a client-side viewer action.

        The daemon never opens a window; this returns the same endpoint the
        client viewer should connect to.
        """
        return self.peek_endpoint(seat_id)

    # -- agent-facing file transfer + desktop helpers --------------------
    def copy_in(self, seat_id: int, host_path: str, guest_path: str) -> dict:
        """Copy one host file into the seat (host path constrained to the root)."""
        return self.call("copy_in", seat_id=seat_id, host_path=host_path, guest_path=guest_path)

    def copy_out(self, seat_id: int, guest_path: str, host_path: str) -> dict:
        """Copy one seat file out to the host (host path constrained to the root)."""
        return self.call("copy_out", seat_id=seat_id, guest_path=guest_path, host_path=host_path)

    def launch_app(self, seat_id: int, command: str, *, tui: bool = False) -> dict:
        """Launch an app on the seat's desktop (``tui=True`` for a terminal)."""
        return self.call("launch_app", seat_id=seat_id, command=command, tui=tui)

    def list_windows(self, seat_id: int) -> list:
        """List the seat desktop's windows."""
        return self.call("list_windows", seat_id=seat_id)["windows"]

    def focus_window(self, seat_id: int, match: str) -> dict:
        """Focus the window whose class/title matches ``match`` (regex)."""
        return self.call("focus_window", seat_id=seat_id, match=match)["window"]

    def resize_window(self, seat_id: int, match: str, width: int, height: int) -> dict:
        """Resize the matching window to ``width`` x ``height`` pixels."""
        return self.call("resize_window", seat_id=seat_id, match=match, width=width, height=height)

    def move_window(self, seat_id: int, match: str, x: int, y: int) -> dict:
        """Move the matching window's top-left corner to ``(x, y)``."""
        return self.call("move_window", seat_id=seat_id, match=match, x=x, y=y)

    def float_window(self, seat_id: int, match: str, on: bool = True) -> dict:
        """Turn floating on/off for the matching window."""
        return self.call("float_window", seat_id=seat_id, match=match, on=on)

    def set_theme(self, seat_id: int, name: str) -> dict:
        """Apply the named Omarchy theme."""
        return self.call("set_theme", seat_id=seat_id, name=name)

    def clipboard_get(self, seat_id: int) -> str:
        """Read the seat's Wayland clipboard text."""
        return self.call("clipboard_get", seat_id=seat_id)["text"]

    def clipboard_set(self, seat_id: int, text: str) -> dict:
        """Set the seat's Wayland clipboard text."""
        return self.call("clipboard_set", seat_id=seat_id, text=text)

    def set_admission_override(self, override: str) -> dict:
        return self.call("set_admission_override", override=override)

    def clear_prewarm_backoff(self, seat_type: str | None = None) -> dict:
        return self.call("clear_prewarm_backoff", seat_type=seat_type)

    # -- project images --------------------------------------------------
    def image_list(self) -> list:
        """Registered golden/project images and their bindings."""
        return self.call("image_list")

    def image_build(
        self,
        name: str,
        *,
        recipe: str | None = None,
        base: str | None = None,
        packages: list[str] | None = None,
        post: list[str] | None = None,
        approved: bool = False,
    ) -> JobHandle:
        """Build a project image (long op -> job_id).

        ``approved`` must be true; without it the daemon refuses with
        ``build_not_approved`` and nothing is installed.
        """
        result = self.call(
            "image_build",
            name=name,
            recipe=recipe,
            base=base,
            packages=packages,
            post=post,
            approved=approved,
        )
        return JobHandle(self, result["job_id"], "image_build")

    def image_rm(self, name: str) -> dict:
        """Unregister an image (and delete it when it lives in the store)."""
        return self.call("image_rm", name=name)

    def set_config_value(self, section: str, key: str, value: object) -> dict:
        """Validate and persist one ``section.key = value`` via the daemon.

        The daemon writes the per-user config and returns the applied
        ``{section, key, value, path}``. A bad value raises
        :class:`DaemonRequestError` with ``code == "invalid"``.
        """
        return self.call("set_config_value", section=section, key=key, value=value)

    def set_config_values(self, section: str, values: dict[str, object]) -> dict:
        """Validate and persist several ``section`` keys in one atomic write.

        Used by the GUI's settings dialog so a cross-field change (for example
        raising ``thumbnail_width`` and ``focused_width`` together) is validated
        as a whole instead of being rejected mid-sequence. Returns the daemon's
        ``{section, values, path}`` reply; a bad value raises
        :class:`DaemonRequestError` with ``code == "invalid"``.
        """
        return self.call("set_config_values", section=section, values=values)

    # -- job calls -------------------------------------------------------
    def job_poll(self, job_id: str) -> dict:
        return self.call("job_poll", job_id=job_id)

    def request_seat(
        self,
        agent_label: str,
        seat_type: str,
        *,
        image: str | None = None,
        project: str | None = None,
    ) -> PendingRequest:
        result = self.call(
            "request_seat",
            agent_label=agent_label,
            seat_type=seat_type,
            image=image,
            project=project,
        )
        return PendingRequest(self, result["request_id"])

    def cancel_request(self, request_id: int) -> JobHandle:
        result = self.call("cancel_request", request_id=request_id)
        return JobHandle(self, result["job_id"], "cancel_request")

    def release_seat(
        self,
        seat_id: int,
        *,
        repo: str | None = None,
        export: bool = True,
        branch: str | None = None,
        ref: str | None = None,
    ) -> JobHandle:
        result = self.call(
            "release_seat",
            seat_id=seat_id,
            repo=repo,
            export=export,
            branch=branch,
            ref=ref,
        )
        return JobHandle(self, result["job_id"], "release_seat")

    def reset_seat(self, seat_id: int) -> JobHandle:
        result = self.call("reset_seat", seat_id=seat_id)
        return JobHandle(self, result["job_id"], "reset_seat")

    def export_seat(
        self,
        seat_id: int,
        *,
        repo: str,
        branch: str | None = None,
        ref: str | None = None,
    ) -> JobHandle:
        result = self.call("export_seat", seat_id=seat_id, repo=repo, branch=branch, ref=ref)
        return JobHandle(self, result["job_id"], "export_seat")

    def prepare_repo(self, seat_id: int, spec: RepoSpec | dict) -> JobHandle:
        if isinstance(spec, RepoSpec):
            payload = {"url": spec.url, "branch": spec.branch}
        else:
            payload = spec
        result = self.call("prepare_repo", seat_id=seat_id, spec=payload)
        return JobHandle(self, result["job_id"], "prepare_repo")

    def retry_release(self, seat_id: int) -> JobHandle:
        """Operator action: retry a persisted release intent for a stuck seat."""
        result = self.call("retry_release", seat_id=seat_id)
        return JobHandle(self, result["job_id"], "retry_release")

    def force_discard(self, seat_id: int, *, reason: str = "force_discard") -> JobHandle:
        """Operator escape hatch: destroy a stuck seat's VM (no export)."""
        result = self.call("force_discard", seat_id=seat_id, reason=reason)
        return JobHandle(self, result["job_id"], "force_discard")

    def reconcile(self) -> JobHandle:
        result = self.call("reconcile")
        return JobHandle(self, result["job_id"], "reconcile")


__all__ = [
    "DaemonAlreadyRunning",
    "DaemonClient",
    "DaemonClientError",
    "DaemonNotRunning",
    "DaemonRequestError",
    "DaemonTimeout",
    "JobHandle",
    "PendingRequest",
]
