"""Manager facade: the public Python API for the omavroom daemon.

This is the single seam used by Phase 5's MCP server, the Phase 6 CLI/TUI,
and tests. It wraps the synchronous :class:`~omavroom.manager.scheduler.Scheduler`
and a :class:`~omavroom.manager.provisioner.Provisioner`, and runs the slow
lifecycle work (provision/reset/release/export) on a background worker so
those calls never block.

Desktop read/input ops (``screenshot``/``input``/``peek_endpoint``) are
deliberately **synchronous**: they take the per-seat lock, which serializes
them against reset/release (so they never observe a torn-down or
mid-reset VM), and they are expected to be bounded. Phase 5 may wrap them
in its own executor if a slow SSH round-trip needs to be kept off the
caller's thread.

Public API surface (frozen for Phase 5)
---------------------------------------
Lifecycle::

    Manager(config, db_path=..., provisioner=..., clock=..., free_ram_mb=...,
            export_approver=...)
    manager.start() / manager.stop()   # start() reconciles then runs the worker
    manager.reconcile() -> ReconcileReport   # adopt/reap VMs (also on start)
    manager.tick()                     # one synchronous pass (tests)

Reads (fast, synchronous; bounded by default)::

    manager.list_seats(include_history=False) -> list[SeatView]
    manager.pool_status() -> PoolStatus
    manager.queue_view(include_history=False) -> list[RequestView]
    manager.seat_status(request_id) -> RequestView
    manager.list_execs(seat_id, include_output=True,
                       max_total_output_bytes=8192) -> list[ExecView]
        # bounded aggregate output; None for full per-record rings
    manager.list_events(limit=200) -> list[Event]     # limit=None -> all

Seat requests (non-blocking; returns a handle immediately)::

    handle = manager.request_seat(agent_label, seat_type, image=None, project=None)
    handle.request_id
    handle.status() -> RequestView
    handle.wait_ready(timeout=None) -> RequestView

Lease + work::

    manager.heartbeat(seat_id=...) -> LeaseView      # independent channel
    manager.begin_work(seat_id) / finish_work(seat_id) -> SeatView
    manager.exec_start(seat_id, exec_id, label=None, command=None, timeout_s=None) -> ExecView
    manager.exec_poll(seat_id, exec_id) -> ExecView   # stdout/stderr/exit_code/truncated
    manager.exec_output(seat_id, exec_id, stdout="", stderr="") -> ExecView
    manager.exec_kill(seat_id, exec_id, signal=9) -> ExecView
    manager.exec_finish(seat_id, exec_id, exit_code=None, stdout="", stderr="") -> ExecView

With a ``command``, ``exec_start`` runs it on a dedicated per-exec worker
thread (see :mod:`omavroom.manager.exec_engine`) and streams output into the
bounded ring buffer; without one it is bookkeeping-only. ``exec_kill`` signals
the worker, and release/reset cancel a seat's execs without waiting for them.

Desktop ops (desktop seats; Phase 5 ``screenshot`` / ``input`` / ``peek_*``)::

    manager.screenshot(seat_id, max_width=None, max_bytes=None) -> bytes
    manager.input(seat_id, events: list[InputEvent]) -> None
    manager.peek_endpoint(seat_id) -> str
        # Desktop ops bound their wait for the seat lock; a long
        # export/reset/release yields SeatBusy (wire code ``seat_busy``).

Teardown (non-blocking; returns a handle)::

    manager.release_seat(seat_id, repo=None, export=True) -> Handle
        # fetch -> content gate -> push -> verify SHA -> destroy;
        # fetch/gate/push failure -> held, never destroyed
    manager.reset_seat(seat_id) -> Handle      # revert to clean, keep seat
    manager.cancel_request(request_id) -> Handle  # atomic; claims take release path

Operator recovery (interrupted release/reset; returns a handle)::

    manager.retry_release(seat_id) -> Handle   # re-run the persisted export
    manager.force_discard(seat_id) -> Handle   # destroy, no export (escape hatch)

Admission / prewarm::

    manager.set_admission_override("auto" | "allow" | "deny")
    manager.clear_prewarm_backoff(seat_type=None)

Phase 5 MCP tool mapping:
``pool_status`` -> ``pool_status()`` / ``queue_view()``;
``request_seat``/``seat_status`` -> ``request_seat()`` / ``seat_status()``;
``heartbeat`` -> ``heartbeat()``;
``exec_start``/``exec_poll``/``exec_kill`` -> ``exec_start/exec_poll/exec_kill``;
``screenshot`` -> ``screenshot()``; ``input`` -> ``input()``;
``peek_url``/``peek_attach`` -> ``peek_endpoint()``;
``release_seat`` -> ``release_seat()``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from omavroom import state as st
from omavroom.config import Config
from omavroom.manager.exec_engine import ExecEngine
from omavroom.manager.execs import (
    DEFAULT_LIST_OUTPUT_BUDGET_BYTES,
    ExecTracker,
    ExecView,
)
from omavroom.manager.locks import LockManager, LockTimeout
from omavroom.manager.provisioner import (
    FakeProvisioner,
    InputEvent,
    Provisioner,
    RepoSpec,
)
from omavroom.manager.scheduler import (
    ExportGate,
    LeaseView,
    PoolStatus,
    PumpReport,
    ReconcileReport,
    ReleaseOutcome,
    RequestView,
    Scheduler,
    SeatView,
)

log = logging.getLogger("omavroom.manager")

#: Default seconds a desktop op (screenshot/input/peek) will wait for the
#: per-seat lock before failing with ``seat_busy``. Bounds how long a long
#: export/reset/release can stall an agent's desktop op.
DEFAULT_DESKTOP_OP_LOCK_TIMEOUT_S = 5.0


class SeatBusy(RuntimeError):
    """A seat is exclusively busy (export/reset/release) and did not free.

    Carries the structured wire code ``seat_busy`` so the daemon returns a
    clear, typed error instead of blocking the connection.
    """

    code = "seat_busy"


class Handle:
    """A pending background operation with a blocking ``wait``/``result``."""

    def __init__(self, manager: Manager) -> None:
        self._manager = manager
        self._done = False
        self._result = None
        self._error: BaseException | None = None
        self._event = threading.Event()

    def done(self) -> bool:
        return self._done

    def wait(self, timeout: float | None = None) -> Handle:
        self._manager._wait_for_handle(self, timeout)
        return self

    def result(self, timeout: float | None = None):
        self.wait(timeout)
        if self._error is not None:
            raise self._error
        return self._result

    def error(self) -> BaseException | None:
        return self._error

    def _set_result(self, value) -> None:
        self._result = value
        self._done = True
        self._event.set()

    def _set_error(self, exc: BaseException) -> None:
        self._error = exc
        self._done = True
        self._event.set()


class RequestHandle(Handle):
    """Handle for a queued seat request."""

    def __init__(self, manager: Manager, request_id: int) -> None:
        super().__init__(manager)
        self.request_id = request_id

    def status(self) -> RequestView:
        return self._manager.seat_status(self.request_id)

    def wait_ready(self, timeout: float | None = None) -> RequestView:
        return self._manager.wait_for_request(self.request_id, timeout=timeout)


class Manager:
    """Owns config, durable state, the scheduler, and the background worker."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        db_path: str | Path,
        provisioner: Provisioner | None = None,
        clock=st.utcnow,
        free_ram_mb=None,
        tick_s: float = 0.02,
        export_approver=None,
        desktop_lock_timeout_s: float = DEFAULT_DESKTOP_OP_LOCK_TIMEOUT_S,
    ) -> None:
        self.config = config or Config.default()
        self.store = st.StateStore(db_path)
        self.store.init()
        self.provisioner = provisioner if provisioner is not None else FakeProvisioner()
        self.locks = LockManager()
        self.desktop_lock_timeout_s = desktop_lock_timeout_s
        self.scheduler = Scheduler(
            self.store,
            self.config,
            self.provisioner,
            clock=clock,
            free_ram_mb=free_ram_mb,
            locks=self.locks,
            export_gate=ExportGate(self.config.export, approver=export_approver),
        )
        self.execs = ExecTracker(
            clock=clock,
            max_concurrent_per_seat=self.config.exec.max_concurrent_per_seat,
            max_output_bytes=self.config.exec.max_output_bytes,
            max_concurrent_total=self.config.exec.max_concurrent_total,
        )
        self.exec_engine = ExecEngine(
            self.execs,
            self.provisioner,
            read_seat=lambda seat_id: self.store.read(lambda c: st.seat_by_id(c, seat_id)),
            max_runtime_s=self.config.exec.max_runtime_s,
            on_busy=self._exec_on_busy,
            on_idle=self._exec_on_idle,
        )
        self.tick_s = tick_s
        self.last_pump_error: BaseException | None = None
        self.last_reconcile_report: ReconcileReport | None = None
        self._pending: deque[tuple[Handle, object]] = deque()
        self._tick_lock = threading.Lock()
        self._running = False
        self._stop = False
        self._thread: threading.Thread | None = None
        self._cv = threading.Condition()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Reconcile VMs, then start the background worker."""
        with self._cv:
            if self._running:
                return
            self._running = True
            self._stop = False
        try:
            self.last_reconcile_report = self.reconcile()
        except Exception:  # noqa: BLE001 - start must not die on a bad provisioner
            log.exception("reconcile failed on start; continuing")
        with self._cv:
            self._thread = threading.Thread(
                target=self._worker, name="omavroom-manager", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the background worker (durable state is already committed)."""
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._running = False
        self._thread = None
        self.exec_engine.shutdown()

    def _wake(self) -> None:
        with self._cv:
            self._cv.notify_all()

    def _worker(self) -> None:
        while True:
            with self._cv:
                if self._stop:
                    return
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the pump thread must survive
                log.exception("manager tick failed; worker continuing")
            with self._cv:
                if self._stop:
                    return
                self._cv.wait(self.tick_s)

    def tick(self) -> PumpReport:
        """Run one scheduler pass and execute any queued background jobs."""
        with self._tick_lock:
            try:
                report = self.scheduler.pump()
                self.last_pump_error = None
            except Exception as exc:  # noqa: BLE001 - surfaced via last_pump_error
                self.last_pump_error = exc
                log.exception("scheduler pump failed; continuing to drain jobs")
                report = PumpReport()
            while self._pending:
                handle, fn = self._pending.popleft()
                if handle.done():
                    continue
                try:
                    handle._set_result(fn())
                except BaseException as exc:  # noqa: BLE001 - surfaced via handle
                    handle._set_error(exc)
                finally:
                    handle._event.set()
            return report

    def _submit(self, fn) -> Handle:
        handle = Handle(self)
        self._pending.append((handle, fn))
        self._wake()
        return handle

    def _wait_for_handle(self, handle: Handle, timeout: float | None) -> None:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not handle.done():
            if not self._running:
                self.tick()
                if handle.done():
                    break
            if deadline is not None and time.monotonic() >= deadline:
                break
            if not handle._event.wait(self.tick_s):
                if handle.done():
                    break
        handle._event.wait(timeout=0)

    def run_until_idle(self, *, max_passes: int = 1000) -> int:
        """Pump until no progress is possible (tests / synchronous drivers)."""
        passes = 0
        while passes < max_passes:
            report = self.tick()
            passes += 1
            if report.total == 0 and not self._pending:
                break
        return passes

    def reconcile(self) -> ReconcileReport:
        """Adopt/reap provisioner VMs against durable seat rows.

        Serialized with the scheduler pump under the tick lock, so it is safe
        at runtime, not just on start. The scheduler additionally refuses to
        treat a not-yet-persisted in-flight VM as an orphan.
        """
        with self._tick_lock:
            return self.scheduler.reconcile(self.provisioner.list_vms())

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def list_seats(self, *, include_history: bool = False) -> list[SeatView]:
        return self.scheduler.list_seats(include_history=include_history)

    def pool_status(self) -> PoolStatus:
        return self.scheduler.pool_status()

    def queue_view(self, *, include_history: bool = False) -> list[RequestView]:
        return self.scheduler.queue_view(include_history=include_history)

    def seat_status(self, request_id: int) -> RequestView:
        return self.scheduler.request_view(request_id)

    def list_events(self, *, limit: int = 200):
        return self.scheduler.list_events(limit=limit)

    # ------------------------------------------------------------------
    # requests / admission
    # ------------------------------------------------------------------
    def request_seat(
        self,
        agent_label: str,
        seat_type: str,
        *,
        image: str | None = None,
        project: str | None = None,
    ) -> RequestHandle:
        """Enqueue a seat request and return immediately (never blocks)."""
        request_id = self.scheduler.submit_request(
            agent_label, seat_type, image=image, project=project
        )
        handle = RequestHandle(self, request_id)
        self._wake()
        return handle

    def wait_for_request(
        self,
        request_id: int,
        *,
        timeout: float | None = None,
        until_seat_states: tuple[str, ...] = (
            st.SeatState.READY.value,
            st.SeatState.BUSY.value,
            st.SeatState.HELD.value,
            st.SeatState.ERROR.value,
        ),
    ) -> RequestView:
        """Block until the request is served or fails (pumps when not running)."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            view = self.seat_status(request_id)
            if view.status in st.TERMINAL_REQUEST_STATES:
                return view
            if view.seat is not None and view.seat.state in until_seat_states:
                return view
            if deadline is not None and time.monotonic() >= deadline:
                return view
            if not self._running:
                self.tick()
            else:
                time.sleep(self.tick_s)

    def cancel_request(self, request_id: int) -> Handle:
        """Atomically cancel a request on the worker; returns a Handle."""

        def job() -> RequestView:
            self.scheduler.cancel_request(request_id)
            return self.seat_status(request_id)

        return self._submit(job)

    def set_admission_override(self, override: str) -> None:
        self.scheduler.set_admission_override(override)

    def clear_prewarm_backoff(self, seat_type: str | None = None) -> None:
        self.scheduler.clear_prewarm_backoff(seat_type)

    # ------------------------------------------------------------------
    # lease / work
    # ------------------------------------------------------------------
    def heartbeat(
        self,
        *,
        seat_id: int | None = None,
        request_id: int | None = None,
        lease_id: int | None = None,
    ) -> LeaseView:
        return self.scheduler.heartbeat(seat_id=seat_id, request_id=request_id, lease_id=lease_id)

    def begin_work(self, seat_id: int) -> SeatView:
        return self.scheduler.begin_work(seat_id)

    def finish_work(self, seat_id: int) -> SeatView:
        return self.scheduler.finish_work(seat_id)

    # ------------------------------------------------------------------
    # exec tracking hooks
    # ------------------------------------------------------------------
    def exec_start(
        self,
        seat_id: int,
        exec_id: str,
        *,
        label: str | None = None,
        command: str | None = None,
        timeout_s: int | None = None,
    ) -> ExecView:
        """Start an exec; with a ``command`` a dedicated worker runs it.

        Seat/VM validation and worker management live in :class:`ExecEngine`
        so the daemon handler and this facade stay thin.
        """
        return self.exec_engine.start(
            seat_id, exec_id, label=label, command=command, timeout_s=timeout_s
        )

    def exec_poll(self, seat_id: int, exec_id: str) -> ExecView:
        return self.exec_engine.poll(seat_id, exec_id)

    def exec_output(
        self, seat_id: int, exec_id: str, *, stdout: str = "", stderr: str = ""
    ) -> ExecView:
        """Append streamed output to a running exec (Phase 5 exec streaming)."""
        return self.exec_engine.record_output(seat_id, exec_id, stdout=stdout, stderr=stderr)

    def exec_kill(self, seat_id: int, exec_id: str, *, signal: int = 9) -> ExecView:
        """Kill a running exec; last live exec returns the seat to ready."""
        return self.exec_engine.kill(seat_id, exec_id, signal=signal)

    def exec_finish(
        self,
        seat_id: int,
        exec_id: str,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> ExecView:
        return self.exec_engine.finish(
            seat_id, exec_id, exit_code=exit_code, stdout=stdout, stderr=stderr
        )

    def list_execs(
        self,
        seat_id: int,
        *,
        include_output: bool = True,
        max_total_output_bytes: int | None = DEFAULT_LIST_OUTPUT_BUDGET_BYTES,
    ) -> list[ExecView]:
        """Bounded exec list (metadata by default; full output on request).

        The aggregate stdout+stderr across the list is capped so a long
        history of large execs cannot produce a multi-MiB response. Use
        ``exec_poll`` for one exec's full (ring-bounded) output.
        """
        return self.exec_engine.list(
            seat_id,
            include_output=include_output,
            max_total_output_bytes=max_total_output_bytes,
        )

    def _exec_on_busy(self, seat_id: int) -> None:
        """First live exec flips the seat ``ready -> busy``."""
        if self.execs.active_count(seat_id) == 1:
            self.scheduler.begin_work(seat_id)

    def _exec_on_idle(self, seat_id: int) -> None:
        """Last live exec flips the seat ``busy -> ready``."""
        if self.execs.active_count(seat_id) == 0:
            self.scheduler.finish_work(seat_id)

    # ------------------------------------------------------------------
    # desktop ops (synchronous; serialized per seat against reset/release)
    # ------------------------------------------------------------------
    @contextmanager
    def _seat_guard(self, seat_id: int, *, timeout: float | None = None):
        """Yield the seat's live VM ref while holding its per-seat lock.

        With ``timeout`` the acquisition is bounded and raises
        :class:`SeatBusy` (wire code ``seat_busy``) if a long export/reset/
        release holds the seat, instead of blocking the caller indefinitely.
        """
        seat = self.store.read(lambda c: st.seat_by_id(c, seat_id))
        if seat is None:
            raise KeyError(f"no such seat: {seat_id}")
        try:
            with self.locks.seat(seat.name, timeout=timeout):
                fresh = self.store.read(lambda c: st.seat_by_id(c, seat_id))
                if fresh is None or fresh.vm_name is None:
                    raise RuntimeError(f"seat {seat_id} has no VM")
                yield fresh.vm_name
        except LockTimeout as exc:
            raise SeatBusy(f"seat {seat_id} is busy (export/reset/release in progress)") from exc

    def screenshot(
        self, seat_id: int, *, max_width: int | None = None, max_bytes: int | None = None
    ) -> bytes:
        with self._seat_guard(seat_id, timeout=self.desktop_lock_timeout_s) as vm_ref:
            return self.provisioner.screenshot(vm_ref, max_width=max_width, max_bytes=max_bytes)

    def input(self, seat_id: int, events: list[InputEvent]) -> None:
        with self._seat_guard(seat_id, timeout=self.desktop_lock_timeout_s) as vm_ref:
            self.provisioner.input(vm_ref, events)

    def peek_endpoint(self, seat_id: int) -> str:
        with self._seat_guard(seat_id, timeout=self.desktop_lock_timeout_s) as vm_ref:
            return self.provisioner.peek_endpoint(vm_ref)

    # ------------------------------------------------------------------
    # teardown (background)
    # ------------------------------------------------------------------
    def release_seat(
        self,
        seat_id: int,
        *,
        repo: str | None = None,
        export: bool = True,
        branch: str | None = None,
        ref: str | None = None,
    ) -> Handle:
        """Fetch -> gate -> push -> destroy the seat VM on the worker.

        ``branch``/``ref`` are forwarded to the export so a manager-mediated
        release-with-export reaches the provisioner's explicit push target
        (Phase 4B1 delta: without them the ExportSpec had no branch and the
        export failed as "invalid export branch").
        """

        def job() -> ReleaseOutcome:
            return self.scheduler.release_seat(
                seat_id, repo=repo, export=export, branch=branch, ref=ref
            )

        # Cancel execs promptly (non-blocking); the release itself never waits
        # on a worker, so a long exec can never delay teardown.
        self.exec_engine.cancel_seat(seat_id)
        return self._submit(job)

    def reset_seat(self, seat_id: int) -> Handle:
        """Revert a seat to its golden overlay on the worker."""

        def job() -> SeatView:
            return self.scheduler.reset_seat(seat_id)

        # Reset discards the VM, so any in-flight exec is invalid.
        self.exec_engine.cancel_seat(seat_id)
        return self._submit(job)

    def prepare_repo(self, seat_id: int, spec: RepoSpec) -> Handle:
        """Inject a repository into a ready seat on the worker."""

        def job() -> None:
            with self._seat_guard(seat_id) as vm_ref:
                self.provisioner.prepare_repo(vm_ref, spec)

        return self._submit(job)

    def export_seat(
        self,
        seat_id: int,
        *,
        repo: str,
        branch: str | None = None,
        ref: str | None = None,
    ) -> Handle:
        """Export work from a seat without releasing it, on the worker.

        ``branch``/``ref`` are forwarded to the export spec (Phase 4B1 delta).
        """
        return self._submit(
            lambda: self.scheduler.export_seat(seat_id, repo=repo, branch=branch, ref=ref)
        )

    def retry_release(self, seat_id: int) -> Handle:
        """Operator action: retry a persisted release intent on the worker.

        Unblocks a seat left ``releasing``/``held`` by an interrupted or
        failed export. Raises ``ProvisionerError`` when no intent exists.
        """
        return self._submit(lambda: self.scheduler.retry_release(seat_id))

    def force_discard(self, seat_id: int, *, reason: str = "force_discard") -> Handle:
        """Operator escape hatch: destroy a stuck seat's VM on the worker."""
        self.exec_engine.cancel_seat(seat_id)
        return self._submit(lambda: self.scheduler.force_discard(seat_id, reason=reason))


# Re-exported so callers import everything from the package root.
__all__ = [
    "DEFAULT_DESKTOP_OP_LOCK_TIMEOUT_S",
    "ExecEngine",
    "ExecTracker",
    "ExportGate",
    "Handle",
    "InputEvent",
    "LeaseView",
    "LockManager",
    "Manager",
    "PoolStatus",
    "PumpReport",
    "ReconcileReport",
    "ReleaseOutcome",
    "RepoSpec",
    "RequestHandle",
    "RequestView",
    "Scheduler",
    "SeatBusy",
    "SeatView",
]
