"""omavroom manager daemon (Phase 4B2): long-running Manager + local socket API.

The daemon owns exactly one :class:`~omavroom.manager.Manager` and serves a
newline-delimited JSON protocol over a Unix-domain socket. It is the single
process authorized to touch libvirt/QEMU; Phase 5's MCP server and Phase 6's
CLI/TUI are clients of this socket (via :mod:`omavroom.client`).

Transport
---------
- Socket: ``$XDG_RUNTIME_DIR/omavroom/daemon.sock`` when ``XDG_RUNTIME_DIR``
  is set, else ``~/.local/state/omavroom/daemon.sock``. The containing
  directory is mode ``0700`` and the socket mode ``0600``.
- Single instance: the socket is bound exclusively. If the path already
  exists the daemon probes it; a live listener refuses startup
  (:class:`DaemonAlreadyRunning`), a stale socket is unlinked and rebound.
- Protocol version **1**: one JSON object per line, request
  ``{"id", "method", "params"}`` and response ``{"id", "ok", "result"}`` or
  ``{"id": <echoed id>, "ok": false, "error": {"code", "message"}}``.
  ``ping`` returns ``{"pong": true, "protocol": 1}``. One connection handler
  thread per client; many concurrent clients are supported, bounded by a
  max-connections cap, a max request-line size and a read timeout.
- Errors are always structured JSON; tracebacks are logged server-side and
  never sent to a client.

Phase 5 mapping / frozen contract
---------------------------------
- ``exec_start`` accepts ``command`` (+ optional ``timeout_s``) and
  ``exec_poll`` returns ``stdout``/``stderr``/``exit_code``/``truncated``
  from bounded (ring) buffers; ``exec_output`` streams into the buffer and
  ``exec_finish`` may carry the final output.
- A ``command`` exec is executed for real by the manager's
  :class:`~omavroom.manager.exec_engine.ExecEngine` on a **dedicated per-exec
  worker thread** (not the daemon job queue), so a long command never delays
  lifecycle work. Without a ``command`` the exec stays bookkeeping-only and
  the client drives it with ``exec_output``/``exec_finish`` (v1 compatibility).
- ``screenshot`` defaults to ``DEFAULT_SCREENSHOT_MAX_WIDTH`` and clamps to
  ``MAX_SCREENSHOT_MAX_WIDTH`` so a response can never be a multi-MB line.
- ``peek_url`` (MCP name) maps to ``peek_endpoint``; ``peek_attach`` is a
  **client-side viewer action** — the daemon returns the ``vnc://`` endpoint
  and never opens a viewer/window itself. The client offers ``peek_url`` /
  ``peek_attach`` aliases that both resolve the endpoint.

Long operations
---------------
Lifecycle work that can take seconds or minutes (provisioning a seat,
release-with-export, reset, prepare repo, reconcile) is *never* executed on
the connection thread. The request returns a ``job_id`` immediately and the
client polls ``job_poll``; the daemon's job worker thread drains the jobs.
Fast reads (status/queue/event views) execute inline.

Lifecycle / reconcile policy
----------------------------
- On start the daemon calls :meth:`Manager.reconcile` (through
  :meth:`Manager.start`) and logs the report. With the real
  :class:`LibvirtProvisioner` this adopts running seat domains whose rows
  survived in sqlite and reaps orphans; with :class:`FakeProvisioner` it is
  a no-op. The template autostart invariant is verified when the provisioner
  supports it.
- On SIGTERM/SIGINT the daemon stops accepting, stops the job worker, stops
  the manager worker, closes the socket and exits. It never destroys a
  running seat: durable state lives in sqlite, so the next daemon start
  reattaches to whatever was left running. A seat is only destroyed by an
  explicit ``release_seat``/``reset``/reclaim.
"""

from __future__ import annotations

import base64
import dataclasses
import itertools
import json
import logging
import os
import queue
import signal
import socket
import sys
import threading
from pathlib import Path

from omavroom.config import Config, set_config_value
from omavroom.manager import Manager
from omavroom.manager.execs import DEFAULT_LIST_OUTPUT_BUDGET_BYTES
from omavroom.manager.provisioner import (
    FakeProvisioner,
    InputEvent,
    Provisioner,
    ProvisionerError,
    RepoSpec,
)

log = logging.getLogger("omavroom.daemon")

DEFAULT_SOCKET_NAME = "daemon.sock"
DEFAULT_STATE_DB = "state.db"
DEFAULT_LOG_NAME = "daemon.log"
DIR_MODE = 0o700
SOCKET_MODE = 0o600
LISTEN_BACKLOG = 64
ACCEPT_TIMEOUT_S = 0.5

#: Wire protocol version, echoed by ``ping``.
PROTOCOL_VERSION = 1
#: Provisioner names accepted by the daemon/CLI (``real`` is an alias).
PROVISIONER_CHOICES: tuple[str, ...] = ("libvirt", "real", "fake")
#: Screenshot contract: default downscale width and the hard cap the server
#: enforces so a screenshot can never produce a multi-MB JSON line.
DEFAULT_SCREENSHOT_MAX_WIDTH = 1024
MAX_SCREENSHOT_MAX_WIDTH = 2048
#: Screenshot byte caps: the encoded PNG is bounded as well as its width, so a
#: response can never be a multi-MB JSON line even on a busy framebuffer.
DEFAULT_SCREENSHOT_MAX_BYTES = 2_000_000
MAX_SCREENSHOT_MAX_BYTES = 8_000_000
#: Connection guard rails (advisories): max request line, read timeout, cap
#: on simultaneous client connections.
MAX_LINE_BYTES = 1024 * 1024
READ_TIMEOUT_S = 300.0
MAX_CONNECTIONS = 64
#: Bounded job history so a long-lived daemon cannot grow without limit.
MAX_JOBS = 256


# --------------------------------------------------------------------------
# paths / logging
# --------------------------------------------------------------------------
def default_state_dir() -> Path:
    """``$XDG_STATE_HOME/omavroom`` or ``~/.local/state/omavroom``."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "omavroom"


def default_socket_dir() -> Path:
    """Runtime directory for the socket, falling back to the state dir."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "omavroom" if runtime else default_state_dir()


def default_socket_path() -> Path:
    """Default Unix socket path for the daemon."""
    return default_socket_dir() / DEFAULT_SOCKET_NAME


def default_state_db() -> Path:
    """Default sqlite state database path."""
    return default_state_dir() / DEFAULT_STATE_DB


def default_log_path() -> Path:
    """Default daemon log path."""
    return default_state_dir() / DEFAULT_LOG_NAME


def configure_logging(log_path: str | Path, level: str | None = None) -> None:
    """Log to stderr and ``log_path``; level from env ``OMAVROOM_LOG_LEVEL``."""
    resolved = (level or os.environ.get("OMAVROOM_LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, resolved, logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:  # pragma: no cover - logging must never take the daemon down
        log.warning("cannot open daemon log %s; logging to stderr only", log_path, exc_info=True)


# --------------------------------------------------------------------------
# errors + JSON conversion
# --------------------------------------------------------------------------
class DaemonError(Exception):
    """Base class for daemon-protocol failures."""

    code = "daemon_error"


class DaemonAlreadyRunning(DaemonError):
    """Raised when another daemon owns the socket."""

    code = "already_running"


class UnknownMethod(DaemonError):
    """Raised when a request names a method the daemon does not implement."""

    code = "unknown_method"


def _error_payload(exc: BaseException) -> dict[str, str]:
    """Map an exception to a structured ``{code, message}`` (no traceback)."""
    if isinstance(exc, DaemonError):
        code = exc.code
    elif isinstance(exc, KeyError):
        code = "not_found"
    elif isinstance(getattr(exc, "code", None), str):
        # Domain errors (e.g. ExecNotAllowed -> ``exec_not_allowed``) carry
        # their own structured code without becoming daemon-layer classes.
        code = exc.code
    elif isinstance(exc, ValueError):
        code = "invalid"
    elif isinstance(exc, ProvisionerError):
        code = "provisioner_error"
    elif isinstance(exc, RuntimeError):
        code = "runtime_error"
    else:
        code = "internal_error"
    message = str(exc) or exc.__class__.__name__
    return {"code": code, "message": message}


def to_wire(value):
    """Convert dataclasses/bytes/enums to JSON-serializable structures.

    The ``bytes`` branch is a defensive fallback (e.g. a future dataclass
    carrying binary data); the only binary response today, ``screenshot``,
    base64-encodes explicitly into ``png_base64`` rather than relying on it.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"__bytes_base64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple, set)):
        return [to_wire(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_wire(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result = {
            field.name: to_wire(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
        # Computed properties (e.g. ``ExportOutcome.ok``/``.message``) are not
        # dataclass fields; surface public ones so the wire view is complete.
        for name, attribute in vars(type(value)).items():
            if name.startswith("_") or name in result:
                continue
            if isinstance(attribute, property) and attribute.fget is not None:
                try:
                    result[name] = to_wire(attribute.fget(value))
                except Exception:  # noqa: BLE001 - a bad property must not break the wire
                    continue
        return result
    return str(value)


# --------------------------------------------------------------------------
# job registry (long ops drained by one worker thread)
# --------------------------------------------------------------------------
@dataclasses.dataclass
class Job:
    id: str
    method: str
    state: str = "pending"
    result: object = None
    error: dict | None = None


class JobRegistry:
    """Thread-safe registry + single worker for long-running operations.

    *Finished* job history is bounded to ``max_jobs``: once full, the oldest
    finished jobs are evicted (pending/running jobs are never dropped).
    Evicted job ids simply poll as ``not_found``. Pending jobs are **not**
    bounded here — they are bounded indirectly by ``exec.max_concurrent_per_seat``
    and by the manager serializing lifecycle work, and an abusive client could
    still submit many; a per-client job quota is deferred.
    """

    def __init__(self, *, max_jobs: int = MAX_JOBS) -> None:
        if max_jobs < 1:
            raise ValueError("max_jobs must be >= 1")
        self.max_jobs = max_jobs
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._counter = itertools.count(1)
        self._thread: threading.Thread | None = None
        self._stop = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop = False
            self._thread = threading.Thread(
                target=self._worker, name="omavroom-daemon-jobs", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stop = True
        self._queue.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    def submit(self, method: str, fn) -> str:
        job_id = f"job-{next(self._counter)}"
        job = Job(id=job_id, method=method)
        with self._lock:
            self._jobs[job_id] = job
            self._prune_locked()
        self._queue.put((job, fn))
        return job_id

    def _prune_locked(self) -> None:
        if len(self._jobs) <= self.max_jobs:
            return
        # dict preserves insertion order; evict the oldest finished job(s).
        for key, job in list(self._jobs.items()):
            if len(self._jobs) <= self.max_jobs:
                break
            if job.state != "pending":
                del self._jobs[key]

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"no such job: {job_id}")
        return job

    def size(self) -> int:
        """Number of retained job records (bounded by ``max_jobs``)."""
        with self._lock:
            return len(self._jobs)

    def view(self, job_id: str) -> dict:
        job = self.get(job_id)
        with self._lock:
            payload: dict = {"job_id": job.id, "method": job.method, "state": job.state}
            if job.state == "done":
                payload["result"] = job.result
            elif job.state == "error":
                payload["error"] = job.error
        return payload

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, fn = item
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001 - reported through the job
                log.exception("job %s (%s) failed", job.id, job.method)
                with self._lock:
                    job.error = _error_payload(exc)
                    job.state = "error"
                    self._prune_locked()
            else:
                with self._lock:
                    job.result = result
                    job.state = "done"
                    self._prune_locked()


# --------------------------------------------------------------------------
# protocol handlers
# --------------------------------------------------------------------------
def _required(params: dict, key: str) -> object:
    if key not in params or params[key] is None:
        raise ValueError(f"missing required parameter: {key!r}")
    return params[key]


def _required_int(params: dict, key: str) -> int:
    value = _required(params, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"parameter {key!r} must be an integer")
    return value


def _required_str(params: dict, key: str) -> str:
    value = _required(params, key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"parameter {key!r} must be a non-empty string")
    return value


def _optional_bool(params: dict, key: str, default: bool) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"parameter {key!r} must be a boolean")
    return value


def _optional_int(params: dict, key: str, default):
    value = params.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"parameter {key!r} must be an integer or null")
    return value


def _optional_str(params: dict, key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"parameter {key!r} must be a string or null")
    return value


class Protocol:
    """The wire method table over a Manager (plus the job registry)."""

    def __init__(self, manager: Manager, jobs: JobRegistry) -> None:
        self.manager = manager
        self.jobs = jobs
        self._methods = {
            "ping": self._ping,
            "pool_status": self._pool_status,
            "queue_view": self._queue_view,
            "seat_status": self._seat_status,
            "list_seats": self._list_seats,
            "list_events": self._list_events,
            "list_execs": self._list_execs,
            "heartbeat": self._heartbeat,
            "begin_work": self._begin_work,
            "finish_work": self._finish_work,
            "exec_start": self._exec_start,
            "exec_poll": self._exec_poll,
            "exec_output": self._exec_output,
            "exec_finish": self._exec_finish,
            "exec_kill": self._exec_kill,
            "screenshot": self._screenshot,
            "input": self._input,
            "peek_endpoint": self._peek_endpoint,
            "set_admission_override": self._set_admission_override,
            "set_config_value": self._set_config_value,
            "clear_prewarm_backoff": self._clear_prewarm_backoff,
            "request_seat": self._request_seat,
            "cancel_request": self._cancel_request,
            "release_seat": self._release_seat,
            "reset_seat": self._reset_seat,
            "export_seat": self._export_seat,
            "prepare_repo": self._prepare_repo,
            "retry_release": self._retry_release,
            "force_discard": self._force_discard,
            "reconcile": self._reconcile,
            "job_poll": self._job_poll,
        }

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(self._methods)

    def dispatch(self, method: str, params: dict) -> object:
        handler = self._methods.get(method)
        if handler is None:
            raise UnknownMethod(f"unknown method: {method!r}")
        return handler(params)

    # -- fast reads ------------------------------------------------------
    def _ping(self, params: dict) -> dict:
        return {"pong": True, "protocol": PROTOCOL_VERSION}

    def _pool_status(self, params: dict):
        return self.manager.pool_status()

    def _queue_view(self, params: dict):
        return self.manager.queue_view(
            include_history=_optional_bool(params, "include_history", False)
        )

    def _seat_status(self, params: dict):
        return self.manager.seat_status(_required_int(params, "request_id"))

    def _list_seats(self, params: dict):
        return self.manager.list_seats(
            include_history=_optional_bool(params, "include_history", False)
        )

    def _list_events(self, params: dict):
        return self.manager.list_events(limit=_optional_int(params, "limit", 200))

    def _list_execs(self, params: dict):
        # Safe bounded defaults; a client may ask for metadata only
        # (``include_output=false``) or the full per-record rings
        # (``max_total_output_bytes=null``).
        max_bytes = _optional_int(
            params, "max_total_output_bytes", DEFAULT_LIST_OUTPUT_BUDGET_BYTES
        )
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("parameter 'max_total_output_bytes' must be >= 0 or null")
        return self.manager.list_execs(
            _required_int(params, "seat_id"),
            include_output=_optional_bool(params, "include_output", True),
            max_total_output_bytes=max_bytes,
        )

    # -- lease / work ----------------------------------------------------
    def _heartbeat(self, params: dict):
        return self.manager.heartbeat(
            seat_id=_optional_int(params, "seat_id", None),
            request_id=_optional_int(params, "request_id", None),
            lease_id=_optional_int(params, "lease_id", None),
        )

    def _begin_work(self, params: dict):
        return self.manager.begin_work(_required_int(params, "seat_id"))

    def _finish_work(self, params: dict):
        return self.manager.finish_work(_required_int(params, "seat_id"))

    # -- exec ------------------------------------------------------------
    def _exec_start(self, params: dict):
        # Frozen Phase 5 contract: accept the command (and optional timeout);
        # the manager's exec engine executes it on a per-exec worker thread.
        return self.manager.exec_start(
            _required_int(params, "seat_id"),
            _required_str(params, "exec_id"),
            label=_optional_str(params, "label"),
            command=_optional_str(params, "command"),
            timeout_s=_optional_int(params, "timeout_s", None),
        )

    def _exec_poll(self, params: dict):
        return self.manager.exec_poll(
            _required_int(params, "seat_id"), _required_str(params, "exec_id")
        )

    def _exec_output(self, params: dict):
        return self.manager.exec_output(
            _required_int(params, "seat_id"),
            _required_str(params, "exec_id"),
            stdout=_optional_str(params, "stdout") or "",
            stderr=_optional_str(params, "stderr") or "",
        )

    def _exec_finish(self, params: dict):
        return self.manager.exec_finish(
            _required_int(params, "seat_id"),
            _required_str(params, "exec_id"),
            exit_code=_optional_int(params, "exit_code", None),
            stdout=_optional_str(params, "stdout") or "",
            stderr=_optional_str(params, "stderr") or "",
        )

    def _exec_kill(self, params: dict):
        return self.manager.exec_kill(
            _required_int(params, "seat_id"),
            _required_str(params, "exec_id"),
            signal=_optional_int(params, "signal", 9),
        )

    # -- desktop ops -----------------------------------------------------
    def _screenshot(self, params: dict):
        seat_id = _required_int(params, "seat_id")
        requested = _optional_int(params, "max_width", None)
        if requested is None:
            width = DEFAULT_SCREENSHOT_MAX_WIDTH
        elif requested < 1:
            raise ValueError("parameter 'max_width' must be >= 1")
        else:
            width = min(requested, MAX_SCREENSHOT_MAX_WIDTH)
        requested_bytes = _optional_int(params, "max_bytes", None)
        if requested_bytes is None:
            max_bytes = DEFAULT_SCREENSHOT_MAX_BYTES
        elif requested_bytes < 1:
            raise ValueError("parameter 'max_bytes' must be >= 1")
        else:
            max_bytes = min(requested_bytes, MAX_SCREENSHOT_MAX_BYTES)
        data = self.manager.screenshot(seat_id, max_width=width, max_bytes=max_bytes)
        return {
            "png_base64": base64.b64encode(data).decode("ascii"),
            "max_width": width,
            "hard_cap": MAX_SCREENSHOT_MAX_WIDTH,
            "max_bytes": max_bytes,
            "hard_cap_bytes": MAX_SCREENSHOT_MAX_BYTES,
        }

    def _input(self, params: dict):
        seat_id = _required_int(params, "seat_id")
        raw_events = params.get("events", [])
        if not isinstance(raw_events, list):
            raise ValueError("parameter 'events' must be a list")
        events = []
        for item in raw_events:
            if not isinstance(item, dict):
                raise ValueError("each input event must be an object")
            events.append(InputEvent(kind=item.get("kind"), value=item.get("value")))
        self.manager.input(seat_id, events)
        return {"applied": len(events)}

    def _peek_endpoint(self, params: dict):
        return {"endpoint": self.manager.peek_endpoint(_required_int(params, "seat_id"))}

    # -- admission -------------------------------------------------------
    def _set_admission_override(self, params: dict):
        override = _required_str(params, "override")
        self.manager.set_admission_override(override)
        return {"override": override}

    def _clear_prewarm_backoff(self, params: dict):
        seat_type = _optional_str(params, "seat_type")
        self.manager.clear_prewarm_backoff(seat_type)
        return {"seat_type": seat_type}

    # -- config ----------------------------------------------------------
    def _set_config_value(self, params: dict) -> dict:
        # Validate through the same schema as file loading, persist to the
        # per-user config (preserving every other key), then apply it to the
        # running config so subsequent reads agree. A bad value raises
        # ValueError -> the ``invalid`` error code.
        section = _required_str(params, "section")
        key = _required_str(params, "key")
        if "value" not in params:
            raise ValueError("missing required parameter: 'value'")
        value = params["value"]
        coerced, path = set_config_value(section, key, value)
        self.manager.config.set_value(section, key, coerced)
        return {
            "section": section,
            "key": key,
            "value": coerced,
            "path": str(path),
        }

    # -- long ops (job) --------------------------------------------------
    def _request_seat(self, params: dict) -> dict:
        # Requesting is itself non-blocking (the scheduler enqueues atomically
        # and claims on the manager pump), so this returns the request id
        # immediately; clients follow it with ``seat_status``. It is
        # deliberately *not* a blocking job, so one agent waiting for a seat
        # can never occupy a daemon job worker.
        handle = self.manager.request_seat(
            _required_str(params, "agent_label"),
            _required_str(params, "seat_type"),
            image=_optional_str(params, "image"),
            project=_optional_str(params, "project"),
        )
        return {"request_id": handle.request_id}

    def _cancel_request(self, params: dict) -> dict:
        request_id = _required_int(params, "request_id")
        job_id = self.jobs.submit(
            "cancel_request", lambda: self.manager.cancel_request(request_id).result()
        )
        return {"job_id": job_id}

    def _release_seat(self, params: dict) -> dict:
        seat_id = _required_int(params, "seat_id")
        repo = _optional_str(params, "repo")
        export = _optional_bool(params, "export", True)
        branch = _optional_str(params, "branch")
        ref = _optional_str(params, "ref")
        job_id = self.jobs.submit(
            "release_seat",
            lambda: self.manager.release_seat(
                seat_id, repo=repo, export=export, branch=branch, ref=ref
            ).result(),
        )
        return {"job_id": job_id}

    def _reset_seat(self, params: dict) -> dict:
        seat_id = _required_int(params, "seat_id")
        job_id = self.jobs.submit("reset_seat", lambda: self.manager.reset_seat(seat_id).result())
        return {"job_id": job_id}

    def _export_seat(self, params: dict) -> dict:
        seat_id = _required_int(params, "seat_id")
        repo = _required_str(params, "repo")
        branch = _optional_str(params, "branch")
        ref = _optional_str(params, "ref")
        job_id = self.jobs.submit(
            "export_seat",
            lambda: self.manager.export_seat(seat_id, repo=repo, branch=branch, ref=ref).result(),
        )
        return {"job_id": job_id}

    def _prepare_repo(self, params: dict) -> dict:
        seat_id = _required_int(params, "seat_id")
        spec = params.get("spec")
        if spec is None:
            spec = {"url": params.get("url"), "branch": params.get("branch")}
        if not isinstance(spec, dict):
            raise ValueError("parameter 'spec' must be an object")
        repo_spec = RepoSpec(url=spec.get("url"), branch=spec.get("branch"))
        job_id = self.jobs.submit(
            "prepare_repo", lambda: self.manager.prepare_repo(seat_id, repo_spec).result()
        )
        return {"job_id": job_id}

    def _retry_release(self, params: dict) -> dict:
        seat_id = _required_int(params, "seat_id")
        job_id = self.jobs.submit(
            "retry_release", lambda: self.manager.retry_release(seat_id).result()
        )
        return {"job_id": job_id}

    def _force_discard(self, params: dict) -> dict:
        seat_id = _required_int(params, "seat_id")
        reason = _optional_str(params, "reason") or "force_discard"
        job_id = self.jobs.submit(
            "force_discard", lambda: self.manager.force_discard(seat_id, reason=reason).result()
        )
        return {"job_id": job_id}

    def _reconcile(self, params: dict) -> dict:
        job_id = self.jobs.submit("reconcile", self.manager.reconcile)
        return {"job_id": job_id}

    def _job_poll(self, params: dict) -> dict:
        return self.jobs.view(_required_str(params, "job_id"))


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------
class DaemonServer:
    """Unix-socket JSON server owning one Manager.

    Use :meth:`start` for a non-blocking start (tests), or
    :meth:`serve_forever` to block until :meth:`shutdown`.
    """

    def __init__(
        self,
        manager: Manager,
        *,
        socket_path: str | Path | None = None,
    ) -> None:
        self.manager = manager
        self.socket_path = Path(socket_path) if socket_path else default_socket_path()
        self.jobs = JobRegistry()
        self.protocol = Protocol(manager, self.jobs)
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._conns: set[socket.socket] = set()
        self._conns_lock = threading.Lock()
        self._bound = False

    # -- lifecycle -------------------------------------------------------
    def bind(self) -> None:
        """Bind the socket, refusing to steal it from a live daemon."""
        directory = self.socket_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, DIR_MODE)
        if self.socket_path.exists():
            if self._socket_alive():
                raise DaemonAlreadyRunning(
                    f"another omavroom daemon is already serving {self.socket_path}"
                )
            self.socket_path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.socket_path))
        except OSError as exc:
            sock.close()
            raise DaemonAlreadyRunning(
                f"cannot bind omavroom socket {self.socket_path}: {exc}"
            ) from exc
        os.chmod(self.socket_path, SOCKET_MODE)
        sock.listen(LISTEN_BACKLOG)
        sock.settimeout(ACCEPT_TIMEOUT_S)
        self._sock = sock
        self._bound = True

    def start(self) -> None:
        """Bind, start the manager + job worker, and begin accepting."""
        if self._bound:
            return
        self._stop.clear()
        self.bind()
        self.jobs.start()
        self.manager.start()
        report = self.manager.last_reconcile_report
        if report is not None:
            log.info(
                "reconcile: orphans_destroyed=%d recovered=%d errored=%d off=%d",
                report.orphans_destroyed,
                report.seats_recovered,
                report.seats_errored,
                report.seats_off,
            )
        try:
            self.manager.provisioner.verify_autostart_invariant()
            log.info("autostart invariant verified on templates and seats")
        except Exception:  # noqa: BLE001 - invariant check must not wedge startup
            log.exception("autostart invariant verification failed")
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="omavroom-daemon-accept", daemon=True
        )
        self._accept_thread.start()

    def serve_forever(self) -> None:
        """Start (if needed) and block until shutdown."""
        self.start()
        self._stop.wait()

    def request_stop(self) -> None:
        """Signal the accept loop to stop (safe from a signal handler)."""
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop accepting, drain workers, close the socket; idempotent."""
        self.request_stop()
        thread = self._accept_thread
        if thread is not None:
            thread.join(timeout)
        self._accept_thread = None
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        self.jobs.stop(timeout)
        self.manager.stop()
        if self._bound:
            try:
                self.socket_path.unlink(missing_ok=True)
            except OSError:
                pass
        self._sock = None
        self._bound = False

    # -- connection handling --------------------------------------------
    def _socket_alive(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.socket_path))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self._conns_lock:
                at_capacity = len(self._conns) >= MAX_CONNECTIONS
                if not at_capacity:
                    self._conns.add(conn)
            if at_capacity:
                log.warning("connection limit %d reached; refusing new client", MAX_CONNECTIONS)
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            handler = threading.Thread(target=self._handle_connection, args=(conn,), daemon=True)
            handler.start()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(READ_TIMEOUT_S)
            with conn, conn.makefile("rwb") as stream:
                while not self._stop.is_set():
                    try:
                        # Read up to the cap. A line whose bytes (including the
                        # trailing newline) equal the cap is accepted; only one
                        # that fills the buffer without a newline is over-long.
                        raw = stream.readline(MAX_LINE_BYTES)
                    except TimeoutError:
                        break
                    except OSError:
                        break
                    if not raw:
                        break
                    if len(raw) >= MAX_LINE_BYTES and not raw.endswith(b"\n"):
                        response = self._error_response(
                            None, "bad_request", "request line too long"
                        )
                        try:
                            stream.write(
                                json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
                            )
                            stream.flush()
                        except OSError:
                            pass
                        break
                    if not raw.strip():
                        continue
                    response = self._handle_line(raw)
                    stream.write(
                        json.dumps(response, separators=(",", ":"), default=str).encode("utf-8")
                        + b"\n"
                    )
                    stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            with self._conns_lock:
                self._conns.discard(conn)

    def _handle_line(self, raw: bytes) -> dict:
        request_id = None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self._error_response(None, "bad_request", "request is not valid UTF-8")
        try:
            request = json.loads(text)
        except json.JSONDecodeError as exc:
            return self._error_response(None, "bad_request", f"invalid JSON: {exc}")
        if not isinstance(request, dict):
            return self._error_response(None, "bad_request", "request must be a JSON object")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            return self._error_response(request_id, "bad_request", "request needs a 'method'")
        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error_response(request_id, "bad_request", "params must be an object")
        try:
            result = self.protocol.dispatch(method, params)
        except BaseException as exc:  # noqa: BLE001 - never leak a traceback
            log.exception("request id=%r method=%r failed", request_id, method)
            payload = _error_payload(exc)
            return self._error_response(request_id, payload["code"], payload["message"])
        return {"id": request_id, "ok": True, "result": to_wire(result)}

    @staticmethod
    def _error_response(request_id, code: str, message: str) -> dict:
        return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def build_provisioner(name: str, config: Config) -> Provisioner:
    """Construct the provisioner named by ``name``.

    Accepted names: ``libvirt``, ``real`` (alias), and ``fake``. Anything else
    raises :class:`ValueError` with the accepted choices.
    """
    normalized = (name or "").strip().lower()
    if normalized == "fake":
        return FakeProvisioner()
    if normalized in ("libvirt", "real"):
        from omavroom.manager.libvirt_provisioner import LibvirtProvisioner

        return LibvirtProvisioner(config)
    raise ValueError(
        f"unknown provisioner {name!r}; choose one of {', '.join(PROVISIONER_CHOICES)}"
    )


def run_daemon(
    *,
    provisioner: str | None = None,
    config: Config | None = None,
    db_path: str | Path | None = None,
    socket_path: str | Path | None = None,
    log_path: str | Path | None = None,
    log_level: str | None = None,
) -> int:
    """Run the daemon in the foreground until SIGTERM/SIGINT. Returns exit code."""
    cfg = config or Config.load()
    name = provisioner or os.environ.get("OMAVROOM_PROVISIONER", "libvirt")
    db = Path(db_path) if db_path else default_state_db()
    db.parent.mkdir(parents=True, exist_ok=True)
    configure_logging(log_path or default_log_path(), log_level)
    try:
        impl = build_provisioner(name, cfg)
    except ValueError as exc:
        # Clean operator error, never a traceback.
        log.error("%s", exc)
        print(f"omavroom daemon: {exc}", file=sys.stderr)
        return 2

    state = "fake" if isinstance(impl, FakeProvisioner) else "libvirt"
    manager = Manager(cfg, db_path=db, provisioner=impl)
    server = DaemonServer(manager, socket_path=socket_path or default_socket_path())
    log.info(
        "starting omavroom daemon (provisioner=%s, socket=%s, db=%s)",
        state,
        server.socket_path,
        db,
    )

    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda num, frame: _on_signal(server, num))
    try:
        try:
            server.start()
        except DaemonAlreadyRunning as exc:
            log.error("%s", exc)
            return 1
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - defensive
        pass
    finally:
        server.shutdown()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    log.info("omavroom daemon stopped")
    return 0


def _on_signal(server: DaemonServer, signum: int) -> None:
    log.info("received signal %s; shutting down", signum)
    server.request_stop()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI shim
    """Minimal ``python -m omavroom.daemon`` entry point."""
    import argparse

    parser = argparse.ArgumentParser(prog="omavroom-daemon")
    parser.add_argument("--provisioner", choices=list(PROVISIONER_CHOICES), default="libvirt")
    parser.add_argument("--socket", default=None, help="override the daemon socket path")
    parser.add_argument("--db", default=None, help="override the state database path")
    args = parser.parse_args(argv)
    return run_daemon(provisioner=args.provisioner, socket_path=args.socket, db_path=args.db)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_SCREENSHOT_MAX_WIDTH",
    "MAX_SCREENSHOT_MAX_WIDTH",
    "PROTOCOL_VERSION",
    "PROVISIONER_CHOICES",
    "DaemonAlreadyRunning",
    "DaemonError",
    "DaemonServer",
    "Job",
    "JobRegistry",
    "Protocol",
    "UnknownMethod",
    "build_provisioner",
    "configure_logging",
    "default_log_path",
    "default_socket_dir",
    "default_socket_path",
    "default_state_db",
    "default_state_dir",
    "run_daemon",
    "to_wire",
]
