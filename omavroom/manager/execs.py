"""In-memory exec tracking hooks for the manager facade (Phase 4).

Actual command execution and output streaming is Phase 5 (``exec_start`` /
``exec_poll`` / ``exec_kill`` over the MCP server). Phase 4 needs the
bookkeeping the scheduler uses to know a seat is actively working — the
first live exec flips a seat ``ready -> busy``, the last one flips it
``busy -> ready`` — plus a bounded registry so an agent cannot grow it
without limit.

Bounds:

- at most ``max_concurrent_per_seat`` *running* execs per seat
  (``exec.max_concurrent_per_seat`` from config); a new start beyond that
  raises ``ValueError``.
- at most ``max_history_per_seat`` retained records per seat; the oldest
  finished/killed records are evicted as new ones arrive.

Keeping this in memory is deliberate — exec handles are ephemeral and die
with the daemon; they are not part of durable state.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from omavroom import state as st

RUNNING = "running"
FINISHED = "finished"
KILLED = "killed"


@dataclass(frozen=True)
class ExecView:
    exec_id: str
    seat_id: int
    label: str | None
    state: str
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    # Frozen Phase 5 contract: exec_start accepts a command (+ timeout) and
    # exec_poll returns bounded stdout/stderr with a truncation flag. Phase
    # 4B2 only keeps the bookkeeping; Phase 5 fills the output via
    # :meth:`ExecTracker.record_output`/``finish``.
    command: str | None = None
    timeout_s: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False


@dataclass
class _ExecRecord:
    exec_id: str
    seat_id: int
    label: str | None
    state: str
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None
    command: str | None = None
    timeout_s: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False

    def view(self) -> ExecView:
        return ExecView(
            exec_id=self.exec_id,
            seat_id=self.seat_id,
            label=self.label,
            state=self.state,
            started_at=self.started_at,
            finished_at=self.finished_at,
            exit_code=self.exit_code,
            command=self.command,
            timeout_s=self.timeout_s,
            stdout=self.stdout,
            stderr=self.stderr,
            truncated=self.truncated,
        )


class ExecTracker:
    """Thread-safe, bounded registry of execs keyed by seat."""

    def __init__(
        self,
        clock=st.utcnow,
        *,
        max_concurrent_per_seat: int = 4,
        max_history_per_seat: int = 100,
        max_output_bytes: int = 1_048_576,
    ) -> None:
        if max_concurrent_per_seat < 1:
            raise ValueError("max_concurrent_per_seat must be >= 1")
        if max_history_per_seat < 1:
            raise ValueError("max_history_per_seat must be >= 1")
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be >= 1")
        self._clock = clock
        self.max_concurrent_per_seat = max_concurrent_per_seat
        self.max_history_per_seat = max_history_per_seat
        self.max_output_bytes = max_output_bytes
        self._execs: dict[int, dict[str, _ExecRecord]] = {}
        self._lock = threading.Lock()

    def _active_unlocked(self, seat_id: int) -> int:
        return sum(1 for record in self._execs.get(seat_id, {}).values() if record.state == RUNNING)

    def _enforce_history_unlocked(self, seat_id: int) -> None:
        seat_execs = self._execs.get(seat_id, {})
        while len(seat_execs) > self.max_history_per_seat:
            oldest_finished = next(
                (key for key, rec in seat_execs.items() if rec.state != RUNNING), None
            )
            if oldest_finished is None:
                break
            del seat_execs[oldest_finished]

    def start(
        self,
        seat_id: int,
        exec_id: str,
        *,
        label: str | None = None,
        command: str | None = None,
        timeout_s: int | None = None,
    ) -> ExecView:
        if not exec_id:
            raise ValueError("exec_id must be non-empty")
        with self._lock:
            seat_execs = self._execs.setdefault(seat_id, {})
            if exec_id in seat_execs:
                raise ValueError(f"duplicate exec id {exec_id!r} for seat {seat_id}")
            if self._active_unlocked(seat_id) >= self.max_concurrent_per_seat:
                raise ValueError(
                    f"seat {seat_id} already has {self.max_concurrent_per_seat} running execs"
                )
            record = _ExecRecord(
                exec_id=exec_id,
                seat_id=seat_id,
                label=label,
                state=RUNNING,
                started_at=st.fmt_time(self._clock()),
                command=command,
                timeout_s=timeout_s,
            )
            seat_execs[exec_id] = record
            self._enforce_history_unlocked(seat_id)
            return record.view()

    def poll(self, seat_id: int, exec_id: str) -> ExecView:
        with self._lock:
            record = self._execs.get(seat_id, {}).get(exec_id)
            if record is None:
                raise KeyError(f"no exec {exec_id!r} for seat {seat_id}")
            return record.view()

    def _append_locked(self, record: _ExecRecord, stdout: str, stderr: str) -> None:
        """Append output to a record, keeping the most recent ``max_output_bytes``.

        Output is treated as a per-stream ring buffer (character-counted, which
        is a conservative proxy for JSON size); dropped bytes set ``truncated``.
        """
        for attribute, chunk in (("stdout", stdout), ("stderr", stderr)):
            if not chunk:
                continue
            combined = getattr(record, attribute) + chunk
            if len(combined) > self.max_output_bytes:
                setattr(record, attribute, combined[-self.max_output_bytes :])
                record.truncated = True
            else:
                setattr(record, attribute, combined)

    def record_output(
        self, seat_id: int, exec_id: str, *, stdout: str = "", stderr: str = ""
    ) -> ExecView:
        """Append streamed output to a running exec's bounded buffers."""
        with self._lock:
            record = self._execs.get(seat_id, {}).get(exec_id)
            if record is None:
                raise KeyError(f"no exec {exec_id!r} for seat {seat_id}")
            self._append_locked(record, stdout, stderr)
            return record.view()

    def _terminate(
        self,
        seat_id: int,
        exec_id: str,
        state: str,
        exit_code: int | None,
        stdout: str = "",
        stderr: str = "",
    ) -> ExecView:
        with self._lock:
            record = self._execs.get(seat_id, {}).get(exec_id)
            if record is None:
                raise KeyError(f"no exec {exec_id!r} for seat {seat_id}")
            self._append_locked(record, stdout, stderr)
            if record.state == RUNNING:
                record.state = state
                record.finished_at = st.fmt_time(self._clock())
                record.exit_code = exit_code
            return record.view()

    def finish(
        self,
        seat_id: int,
        exec_id: str,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> ExecView:
        return self._terminate(seat_id, exec_id, FINISHED, exit_code, stdout, stderr)

    def kill(self, seat_id: int, exec_id: str, *, signal: int = 9) -> ExecView:
        return self._terminate(seat_id, exec_id, KILLED, -abs(signal))

    def active_count(self, seat_id: int) -> int:
        with self._lock:
            return self._active_unlocked(seat_id)

    def list(self, seat_id: int) -> list[ExecView]:
        with self._lock:
            return [record.view() for record in self._execs.get(seat_id, {}).values()]
