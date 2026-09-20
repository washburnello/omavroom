"""Per-exec worker engine for the manager (Phase 5).

Binding invariant
-----------------
**Every command exec runs on its own dedicated worker thread.** Execs are
deliberately *not* placed on the daemon's shared :class:`~omavroom.daemon.JobRegistry`
queue: a long agent command must never occupy a lifecycle job worker, and it
must never delay provisioning, release, reset, or admission. The daemon
handler that reaches this engine is thin; all execution, streaming, timeout
and kill logic lives here and in the manager facade.

Lifecycle of one command exec
-----------------------------
::

    start()  -> validate seat (ready|busy, VM present and running)
             -> ExecTracker.start()  (concurrency cap + duplicate id)
             -> first exec flips the seat ready -> busy
             -> spawn worker thread; return the RUNNING ExecView
    worker   -> provisioner.run(vm_ref, command,
                                timeout_s=..., on_output=..., cancel=...)
             -> each chunk is appended to the bounded ring buffer
             -> final exit_code recorded: finished / killed (cancel)
             -> last exec flips the seat busy -> ready

Kill / timeout
--------------
``kill`` marks the exec ``killed`` immediately (so a poll never waits for the
transport to unwind) and sets the worker's ``cancel`` event. The worker's
``run`` implementation is then responsible for terminating the transport:
with :class:`LibvirtProvisioner` that means killing the local ``ssh``
process, which closes the channel and normally ends the remote command. A
command that daemonises itself inside the guest can outlive the channel --
a documented limitation, not a guarantee. The wall-clock timeout is enforced
twice: the provisioner receives ``timeout_s`` (hard transport timeout, exit
``124``), and the engine clamps the requested timeout to
``exec.max_runtime_s``. That engine ceiling is optional: ``max_runtime_s = 0``
(default) means no engine-wide cap, so a long build/test is never killed by a
fixed timer (the host stays protected by the libvirt CPU/RAM caps and the
output ring). ``timeout_s = 0`` passed to the provisioner means "no timeout".

Output is a per-stream ring buffer (``ExecTracker``): the most recent
``exec.max_output_bytes`` characters are retained and ``truncated`` is set
when older output is dropped.

Seat-state rules
----------------
An exec is only accepted when the seat row exists, has a ``vm_name``, and is
in ``ready`` or ``busy``. For a command exec the VM is additionally confirmed
running through ``provisioner.attach`` (a transient attach error fails open,
since the manager already believes the VM is live). Anything else raises
:class:`ExecNotAllowed` before any state changes.

An exec with no ``command`` is a *bookkeeping* exec: no worker is spawned and
the client drives it with ``exec_output``/``exec_finish`` (the frozen 4B2
behaviour, preserved for protocol v1 compatibility).

Public API
----------
:meth:`ExecEngine.start`, :meth:`poll`, :meth:`record_output`,
:meth:`finish`, :meth:`kill`, :meth:`list`, :meth:`cancel_seat`,
:meth:`active_workers`, :meth:`shutdown`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from omavroom import state as st
from omavroom.manager.execs import (
    DEFAULT_LIST_OUTPUT_BUDGET_BYTES,
    RUNNING,
    ExecNotAllowed,
    ExecTracker,
    ExecView,
)
from omavroom.manager.provisioner import Provisioner

log = logging.getLogger("omavroom.exec")

#: Seat states on which an exec may start.
_EXECUTABLE_SEAT_STATES = (st.SeatState.READY.value, st.SeatState.BUSY.value)


class ExecEngine:
    """Owns per-exec worker threads over a shared :class:`ExecTracker`.

    ``read_seat(seat_id) -> st.Seat | None`` is injected by the manager so
    the engine does not depend on the scheduler or the sqlite store directly.
    ``on_busy``/``on_idle`` are the seat-state hooks (the manager wires them
    to ``scheduler.begin_work``/``finish_work``); they are always called
    while holding no engine lock, and a failure in a hook is logged, never
    allowed to kill a worker.
    """

    def __init__(
        self,
        tracker: ExecTracker,
        provisioner: Provisioner,
        *,
        read_seat: Callable[[int], st.Seat | None],
        max_runtime_s: int = 0,
        default_timeout_s: int = 60,
        on_busy: Callable[[int], None] | None = None,
        on_idle: Callable[[int], None] | None = None,
    ) -> None:
        if max_runtime_s < 0:
            raise ValueError("max_runtime_s must be >= 0 (0 = no ceiling)")
        if default_timeout_s < 1:
            raise ValueError("default_timeout_s must be >= 1")
        self.tracker = tracker
        self.provisioner = provisioner
        self.max_runtime_s = max_runtime_s
        self.default_timeout_s = default_timeout_s
        self._read_seat = read_seat
        self._on_busy = on_busy
        self._on_idle = on_idle
        # (seat_id, exec_id) -> {"cancel": Event, "signal": [int], "thread": Thread}
        self._workers: dict[tuple[int, str], dict] = {}
        # Guards the seat-state hook decisions: the active-count check and the
        # on_busy/on_idle call happen under _state_lock so a late finish can
        # never run finish_work after a new exec has already begun. Lock order
        # is _state_lock -> _lock (never the reverse).
        self._state_lock = threading.Lock()
        self._lock = threading.Lock()
        self._shutdown = False

    # ------------------------------------------------------------------
    # start / validation
    # ------------------------------------------------------------------
    def start(
        self,
        seat_id: int,
        exec_id: str,
        *,
        label: str | None = None,
        command: str | None = None,
        timeout_s: int | None = None,
    ) -> ExecView:
        """Validate the seat, register the exec, and (for a command) run it."""
        seat = self._read_seat(seat_id)
        if seat is None:
            raise KeyError(f"no such seat: {seat_id}")
        if seat.vm_name is None:
            raise ExecNotAllowed(f"seat {seat_id} has no VM to execute on")
        if seat.state not in _EXECUTABLE_SEAT_STATES:
            raise ExecNotAllowed(
                f"seat {seat_id} is {seat.state!r}; exec requires a ready or busy seat"
            )
        if timeout_s is not None and timeout_s < 1:
            raise ValueError("timeout_s must be >= 1")

        effective = 0
        recorded = timeout_s
        if command:
            if timeout_s is not None:
                requested = timeout_s
            elif self.max_runtime_s > 0:
                requested = self.default_timeout_s
            else:
                # No engine ceiling and no explicit per-call timeout: run
                # without one (0 means "no timeout" to the transport).
                requested = 0
            if self.max_runtime_s > 0:
                effective = min(requested, self.max_runtime_s)
            else:
                effective = requested
            recorded = effective
            if not self._vm_running(seat_id, seat.vm_name):
                raise ExecNotAllowed(f"seat {seat_id} VM is not running")

        worker: dict | None = None
        with self._state_lock:
            view = self.tracker.start(
                seat_id, exec_id, label=label, command=command, timeout_s=recorded
            )
            if command:
                # Register the cancel event *before* the slow on_busy hook and
                # before spawning, so a concurrent kill/cancel can never be lost
                # in the start->spawn window.
                worker = {"cancel": threading.Event(), "signal": [9], "thread": None}
                with self._lock:
                    self._workers[(seat_id, exec_id)] = worker
            if self._on_busy is not None and self.tracker.active_count(seat_id) == 1:
                self._safe_hook(self._on_busy, seat_id, "on_busy")
        if command and worker is not None:
            self._launch(seat_id, exec_id, seat.vm_name, command, effective, worker)
        return view

    def _vm_running(self, seat_id: int, vm_ref: str) -> bool:
        """Best-effort liveness check; fails open on a transient error."""
        try:
            info = self.provisioner.attach(vm_ref)
        except Exception:  # noqa: BLE001 - transient control-plane error
            log.warning("attach failed for %s; allowing exec to proceed", vm_ref, exc_info=True)
            return True
        return info is not None and info.state == "running"

    def _safe_hook(self, hook: Callable[[int], None], seat_id: int, name: str) -> None:
        try:
            hook(seat_id)
        except Exception:  # noqa: BLE001 - a hook must never kill a worker
            log.exception("%s hook failed for seat %s", name, seat_id)

    # ------------------------------------------------------------------
    # worker plumbing
    # ------------------------------------------------------------------
    def _launch(
        self,
        seat_id: int,
        exec_id: str,
        vm_ref: str,
        command: str,
        timeout_s: int,
        worker: dict,
    ) -> None:
        """Start the worker unless it was cancelled/shut down before launch.

        A cancel that lands after the worker is registered but before this
        method runs must win: we must not execute a command the client was
        already told is ``killed``. We check three signals: the worker's
        ``cancel`` event, engine shutdown, and the tracker record itself. The
        tracker check closes the window between ``tracker.start()`` returning
        and the worker entry becoming visible to ``_signal_worker`` (a kill in
        that window marks the record KILLED but finds no worker to signal).
        """
        cancelled = False
        with self._lock:
            if (
                worker["cancel"].is_set()
                or self._shutdown
                or not self._tracker_running(seat_id, exec_id)
            ):
                self._workers.pop((seat_id, exec_id), None)
                cancelled = True
            else:
                thread = threading.Thread(
                    target=self._run_worker,
                    args=(seat_id, exec_id, vm_ref, command, timeout_s, worker),
                    name=f"omavroom-exec-{seat_id}-{exec_id}",
                    daemon=True,
                )
                worker["thread"] = thread
        if cancelled:
            self._finish_killed(seat_id, exec_id, worker["signal"][0])
            self._maybe_idle(seat_id)
            return
        thread.start()

    def _tracker_running(self, seat_id: int, exec_id: str) -> bool:
        """True only while the tracker still records the exec as RUNNING.

        A missing record (already evicted) is treated as not-running so we
        never launch work for an exec the client can no longer observe.
        """
        try:
            return self.tracker.poll(seat_id, exec_id).state == RUNNING
        except KeyError:
            return False

    def _run_worker(
        self,
        seat_id: int,
        exec_id: str,
        vm_ref: str,
        command: str,
        timeout_s: int,
        worker: dict,
    ) -> None:
        cancel: threading.Event = worker["cancel"]
        streamed = {"stdout": False, "stderr": False}

        def on_output(stream: str, chunk: str) -> None:
            streamed[stream] = True
            try:
                self.tracker.record_output(seat_id, exec_id, **{stream: chunk})
            except KeyError:  # pragma: no cover - record could be evicted
                pass

        result = None
        error: BaseException | None = None
        try:
            # Second cancel check (belt and braces): a kill that lands after
            # _launch's check but before/around thread.start() must still win.
            if cancel.is_set() or self._shutdown:
                self._finish_killed(seat_id, exec_id, worker["signal"][0])
                return
            try:
                result = self.provisioner.run(
                    vm_ref,
                    command,
                    timeout_s=timeout_s,
                    on_output=on_output,
                    cancel=cancel,
                )
            except BaseException as exc:  # noqa: BLE001 - surfaced as a failed exec
                error = exc
                log.warning("exec %s on seat %s failed", exec_id, seat_id, exc_info=True)
            if cancel.is_set():
                self._finish_killed(seat_id, exec_id, worker["signal"][0])
            elif result is not None:
                if streamed["stdout"] or streamed["stderr"]:
                    # Output already streamed into the ring buffer; do not
                    # re-append the transport's copy (no double-counting).
                    view = self.tracker.finish(
                        seat_id,
                        exec_id,
                        exit_code=result.returncode,
                        stdout="" if streamed["stdout"] else result.stdout,
                        stderr="" if streamed["stderr"] else result.stderr,
                    )
                else:
                    view = self.tracker.finish(
                        seat_id,
                        exec_id,
                        exit_code=result.returncode,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )
                del view
            else:
                message = f"exec failed: {error}" if error is not None else "exec failed"
                self.tracker.finish(seat_id, exec_id, exit_code=-1, stderr=message + "\n")
        finally:
            # Cleanup on every path (early cancel, transport error, normal).
            with self._lock:
                self._workers.pop((seat_id, exec_id), None)
            self._maybe_idle(seat_id)

    def _finish_killed(self, seat_id: int, exec_id: str, signal: int) -> None:
        try:
            self.tracker.kill(seat_id, exec_id, signal=signal)
        except KeyError:  # pragma: no cover - record evicted mid-flight
            pass

    # ------------------------------------------------------------------
    # reads / client-driven finish
    # ------------------------------------------------------------------
    def poll(self, seat_id: int, exec_id: str) -> ExecView:
        return self.tracker.poll(seat_id, exec_id)

    def record_output(
        self, seat_id: int, exec_id: str, *, stdout: str = "", stderr: str = ""
    ) -> ExecView:
        return self.tracker.record_output(seat_id, exec_id, stdout=stdout, stderr=stderr)

    def finish(
        self,
        seat_id: int,
        exec_id: str,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> ExecView:
        view = self.tracker.finish(
            seat_id, exec_id, exit_code=exit_code, stdout=stdout, stderr=stderr
        )
        self._maybe_idle(seat_id)
        return view

    def list(
        self,
        seat_id: int,
        *,
        include_output: bool = True,
        max_total_output_bytes: int | None = DEFAULT_LIST_OUTPUT_BUDGET_BYTES,
    ) -> list[ExecView]:
        return self.tracker.list(
            seat_id,
            include_output=include_output,
            max_total_output_bytes=max_total_output_bytes,
        )

    # ------------------------------------------------------------------
    # kill / cancel
    # ------------------------------------------------------------------
    def kill(self, seat_id: int, exec_id: str, *, signal: int = 9) -> ExecView:
        """Mark an exec killed and signal its worker (non-blocking).

        ``signal`` only sets the recorded ``exit_code`` (``-signal``) and the
        engine's cancel signal; it is **not** delivered to a guest process.
        The transport terminates the local ``ssh`` process, which closes the
        channel and normally ends the remote command.
        """
        view = self.tracker.kill(seat_id, exec_id, signal=signal)
        self._signal_worker(seat_id, exec_id, signal)
        self._maybe_idle(seat_id)
        return view

    def _signal_worker(self, seat_id: int, exec_id: str, signal: int) -> None:
        with self._lock:
            worker = self._workers.get((seat_id, exec_id))
        if worker is not None:
            worker["signal"][0] = signal
            worker["cancel"].set()

    def cancel_seat(self, seat_id: int, *, signal: int = 9) -> int:
        """Kill every running exec on a seat (used before release/reset).

        Returns the number of execs killed. Never blocks on a worker: the
        worker observes ``cancel`` and unwinds on its own thread.
        """
        killed = 0
        # Metadata-only read: never deep-copy multi-MiB output buffers just to
        # find the running exec ids.
        for exec_id in self.tracker.running_exec_ids(seat_id):
            try:
                self.kill(seat_id, exec_id, signal=signal)
            except KeyError:  # pragma: no cover - racing eviction
                continue
            killed += 1
        return killed

    def _maybe_idle(self, seat_id: int) -> None:
        """Flip the seat busy -> ready only if no exec is running.

        The active-count check and the hook call are in the same critical
        section as the start-side registration (``_state_lock``), so a late
        finish can never run ``finish_work`` after a new exec has begun.
        """
        if self._on_idle is None:
            return
        with self._state_lock:
            if self.tracker.active_count(seat_id) == 0:
                self._safe_hook(self._on_idle, seat_id, "on_idle")

    # ------------------------------------------------------------------
    # introspection / lifecycle
    # ------------------------------------------------------------------
    def active_workers(self) -> int:
        """Number of live command workers (tests / diagnostics)."""
        with self._lock:
            return len(self._workers)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel and join every worker (called from ``Manager.stop``)."""
        with self._lock:
            self._shutdown = True
            workers = list(self._workers.values())
        for worker in workers:
            worker["cancel"].set()
        deadline = time.monotonic() + timeout
        for worker in workers:
            thread = worker.get("thread")
            if thread is None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)


__all__ = ["ExecEngine"]
