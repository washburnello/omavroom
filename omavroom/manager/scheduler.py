"""Capacity scheduler for omavroom (Phase 4, VM-free).

The scheduler is the only place seat lifecycle decisions are made. It is
synchronous and deterministic; the manager facade runs :meth:`pump` on a
background worker so agent-facing calls never block.

Lifecycle / transition table
----------------------------
::

    (no seat) --admit--> queued --provision--> provisioning --ready--> ready
    ready --assign lease--> ready (leased) --begin_work--> busy
    busy --finish_work--> ready
    ready/busy --reset--> resetting --ok--> ready
    any --> releasing --destroy--> off
    releasing --export failed--> held
    error --prewarm retry--> queued        (bounded, see [prewarm])
    error --reap--> off                     (VM destroyed, row closed)

Admission algorithm
-------------------
For seat type ``T`` at claim time (evaluated inside the claim transaction):

1. **Static bound.** If occupying seats of ``T`` >= ``seats.T.max_seats``,
   refuse (``max_seats``). Requests queue.
2. **Manual override.** ``admission.override`` may be ``deny`` (refuse
   everything, drain mode) or ``allow`` (skip step 3, still honour step 1).
3. **Live measurement.** Unless disabled (``admission.dynamic = false``),
   admit only if ``free_ram_mb() - host.headroom_floor_mb >=
   resources.T.memory_mb``. Free RAM already accounts for running VMs, so
   no per-seat subtraction is needed.

``min_seats`` is the prewarm floor: the pump provisions idle ready seats up
to the floor (each still subject to admission). Real requests are admitted
before prewarm so prewarm never starves demand. ``error`` seats count
against the floor until reaped, so a broken image cannot grow rows forever;
prewarm retries are bounded by ``[prewarm]`` and then suspended.

Pinned images and idle seats
----------------------------
An idle (ready, unleased) seat is *evictable*: if the queue head needs an
image that no idle seat provides, the pump destroys an idle seat of another
image and provisions the requested one. Without this, a prewarm seat of the
default image would head-of-line-block a pinned-image request forever.

Leases
------
Acquired at admission, not at readiness, so a stuck provision also times
out. ``expires_at`` is an absolute wall-clock cap (never renewed);
``last_heartbeat`` is renewed by the independent heartbeat channel. A lease
is dead when either passes. Reclaim re-checks liveness inside the release
transaction, so a heartbeat that lands during the scan saves the seat.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from omavroom import state as st
from omavroom.config import Config, ExportConfig
from omavroom.manager.locks import LockManager
from omavroom.manager.provisioner import (
    ExportOutcome,
    ExportSpec,
    FetchResult,
    GateDecision,
    Provisioner,
    ProvisionerError,
    PushResult,
    ResourceCaps,
    VmInfo,
)

log = logging.getLogger("omavroom.scheduler")


def read_free_ram_mb() -> int:
    """Best-effort live free host RAM from ``/proc/meminfo`` (MemAvailable).

    Returns 0 when the value cannot be read, which makes dynamic admission
    fail closed (refuse) rather than overcommit. Tests inject their own
    measurement instead of touching the real machine.
    """
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        return 0
    return 0


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str


@dataclass(frozen=True)
class LeaseView:
    id: int
    seat_id: int
    request_id: int | None
    agent_label: str
    acquired_at: str
    expires_at: str
    last_heartbeat: str


@dataclass(frozen=True)
class SeatView:
    id: int
    name: str
    seat_type: str
    state: str
    vm_name: str | None
    image: str
    agent_label: str | None
    last_error: str | None
    attempts: int
    lease_expires_at: str | None
    last_heartbeat: str | None
    pending_action: str | None = None
    needs_attention: bool = False


@dataclass(frozen=True)
class RequestView:
    id: int
    agent_label: str
    seat_type: str
    project: str | None
    image: str
    status: str
    position: int
    seat_id: int | None
    created_at: str
    updated_at: str
    queue_ahead: int
    seat: SeatView | None
    lease: LeaseView | None


@dataclass(frozen=True)
class TypeStatus:
    seat_type: str
    occupying: int
    error_seats: int
    max_seats: int
    min_seats: int
    waiting: int
    admitted: bool
    reason: str


@dataclass(frozen=True)
class PoolStatus:
    free_ram_mb: int
    headroom_floor_mb: int
    admission_override: str
    seats: list[SeatView]
    queue: list[RequestView]
    per_type: dict[str, TypeStatus]
    #: Seat ids an operator may need to act on (``held``, or ``releasing`` /
    #: ``resetting`` whose intent could not be recovered). ``retry_release``
    #: or ``force_discard`` always unblocks them.
    needs_attention: list[int]


@dataclass
class PumpReport:
    """What one scheduler pass changed (used by tests and run_until_idle)."""

    reaped: int = 0
    reclaimed: int = 0
    evicted: int = 0
    assigned: int = 0
    admitted: int = 0
    prewarmed: int = 0
    provisioned: int = 0
    failed: int = 0
    resumed: int = 0

    @property
    def total(self) -> int:
        return (
            self.reaped
            + self.reclaimed
            + self.evicted
            + self.assigned
            + self.admitted
            + self.prewarmed
            + self.provisioned
            + self.failed
            + self.resumed
        )


@dataclass(frozen=True)
class ReleaseOutcome:
    seat_id: int
    destroyed: bool
    held: bool
    export: ExportOutcome | None = None
    message: str = ""


@dataclass(frozen=True)
class ReconcileReport:
    """Result of adopting/reaping VMs after a daemon restart."""

    orphans_destroyed: int = 0
    seats_errored: int = 0
    seats_recovered: int = 0
    seats_off: int = 0
    #: Interrupted releases/resets resumed to a terminal state.
    seats_resumed: int = 0


class ExportGate:
    """Phase 3 content gate applied between fetch and push.

    Denies destructive diffstats, protected-path touches, a non-empty git
    stash, and (optionally) anything lacking manual approval. A denial
    holds the seat; nothing is ever pushed.
    """

    def __init__(
        self,
        config: ExportConfig,
        *,
        approver: Callable[[FetchResult], bool] | None = None,
    ) -> None:
        self.config = config
        self._approver = approver

    def evaluate(self, fetched: FetchResult) -> GateDecision:
        cfg = self.config
        if not fetched.ok:
            return GateDecision(False, fetched.message or "fetch failed")
        if fetched.stash_count > 0:
            return GateDecision(
                False, f"git stash list is not empty ({fetched.stash_count} entr(y/ies))"
            )
        if fetched.files_changed > cfg.max_files_changed:
            return GateDecision(
                False,
                f"too many files changed: {fetched.files_changed} > {cfg.max_files_changed}",
            )
        if fetched.insertions > cfg.max_insertions:
            return GateDecision(
                False,
                f"too many insertions: {fetched.insertions} > {cfg.max_insertions}",
            )
        if fetched.deletions > cfg.max_deletions:
            return GateDecision(
                False,
                f"too many deletions: {fetched.deletions} > {cfg.max_deletions}",
            )
        for path in fetched.changed_paths:
            prefix = cfg.matches_protected(path)
            if prefix is not None:
                return GateDecision(False, f"protected path touched: {path}")
        if cfg.approval_required:
            if self._approver is None or not self._approver(fetched):
                return GateDecision(False, "export requires approval")
        return GateDecision(True, "allowed")


class Scheduler:
    """Atomic, fair seat scheduler over a :class:`StateStore`."""

    def __init__(
        self,
        store: st.StateStore,
        config: Config,
        provisioner: Provisioner,
        *,
        clock=st.utcnow,
        free_ram_mb=None,
        locks: LockManager | None = None,
        export_gate: ExportGate | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.provisioner = provisioner
        self.locks = locks or LockManager()
        self._clock = clock
        self._free_ram_mb = free_ram_mb or read_free_ram_mb
        self.override = config.admission.override
        self._override_lock = threading.Lock()
        self.gate = export_gate or ExportGate(config.export)
        self._prewarm_suspended: set[str] = set()

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return self._clock()

    def _seat_types(self) -> tuple[str, ...]:
        return tuple(self.config.seats)

    def _check_seat_type(self, seat_type: str) -> None:
        if seat_type not in self.config.seats:
            raise ValueError(f"unknown seat type: {seat_type!r}")

    def _resources(self, seat_type: str) -> ResourceCaps:
        cfg = self.config.resources_for(seat_type)
        return ResourceCaps(cfg.cpu_vcpus, cfg.memory_mb, cfg.overlay_max_gb)

    def _effective_image(self, seat_type: str, image: str | None) -> str:
        return image or self.config.image_for(seat_type)

    def _get_seat(self, seat_id: int) -> st.Seat:
        seat = self.store.read(lambda c: st.seat_by_id(c, seat_id))
        if seat is None:
            raise KeyError(f"no such seat: {seat_id}")
        return seat

    def set_admission_override(self, override: str) -> None:
        """Set the runtime manual admission override (``auto|allow|deny``)."""
        if override not in ("auto", "allow", "deny"):
            raise ValueError(f"invalid override: {override!r}")
        with self._override_lock:
            self.override = override

    def clear_prewarm_backoff(self, seat_type: str | None = None) -> None:
        """Re-enable prewarm for one type (or all) after suspension."""
        if seat_type is None:
            self._prewarm_suspended.clear()
        else:
            self._prewarm_suspended.discard(seat_type)

    # ------------------------------------------------------------------
    # admission
    # ------------------------------------------------------------------
    def admission(self, seat_type: str) -> AdmissionDecision:
        """Decide whether one more occupying seat of ``seat_type`` may exist.

        This is the read-only view used for status. Claiming uses
        :meth:`_admission_conn` *inside* its ``BEGIN IMMEDIATE`` transaction
        so two processes can never both pass the check and then both claim.
        """
        return self.store.read(lambda c: self._admission_conn(c, seat_type))

    def _admission_conn(self, conn, seat_type: str) -> AdmissionDecision:
        self._check_seat_type(seat_type)
        occupying = st.count_occupying(conn, seat_type)
        max_seats = self.config.seats[seat_type].max_seats
        if occupying >= max_seats:
            return AdmissionDecision(False, "max_seats")
        with self._override_lock:
            override = self.override
        if override == "deny":
            return AdmissionDecision(False, "override_deny")
        if self.config.admission.dynamic and override != "allow":
            free_mb = self._free_ram_mb()
            headroom = self.config.host.headroom_floor_mb
            need = self.config.resources_for(seat_type).memory_mb
            if free_mb - headroom < need:
                return AdmissionDecision(False, "insufficient_ram")
        return AdmissionDecision(True, "ok")

    # ------------------------------------------------------------------
    # requests
    # ------------------------------------------------------------------
    def submit_request(
        self,
        agent_label: str,
        seat_type: str,
        *,
        image: str | None = None,
        project: str | None = None,
    ) -> int:
        """Enqueue a seat request; returns its request id (position is stable)."""
        self._check_seat_type(seat_type)
        now = st.fmt_time(self._now())
        with self.store.transaction() as conn:
            request_id = st.enqueue_request(
                conn,
                agent_label=agent_label,
                seat_type=seat_type,
                image=image,
                project=project,
                now=now,
            )
            st.log_event(
                conn,
                event_type="request_submitted",
                now=now,
                request_id=request_id,
                agent_label=agent_label,
                detail=f"seat_type={seat_type} project={project} image={image}",
            )
        return request_id

    def cancel_request(self, request_id: int) -> st.Request:
        """Atomically cancel a request; a claimed request takes the release path.

        The waiting transition is decided inside a single transaction, so a
        concurrent claim either wins (and we then release its seat) or loses
        (and the cancel sticks). No request is ever left dangling.
        """
        request = self._get_request(request_id)
        if request.status in st.TERMINAL_REQUEST_STATES:
            return request
        if request.status == st.RequestStatus.WAITING.value:
            now = st.fmt_time(self._now())
            with self.store.transaction() as conn:
                fresh = st.request_by_id(conn, request_id)
                if fresh is not None and fresh.status == st.RequestStatus.WAITING.value:
                    st.update_request(
                        conn,
                        request_id,
                        status=st.RequestStatus.CANCELLED.value,
                        now=now,
                    )
                    st.log_event(
                        conn,
                        event_type="request_cancelled",
                        now=now,
                        request_id=request_id,
                        agent_label=request.agent_label,
                    )
                request = st.request_by_id(conn, request_id)
        if request.status == st.RequestStatus.CLAIMED.value and request.seat_id is not None:
            self.release_seat(
                request.seat_id,
                export=False,
                request_status=st.RequestStatus.CANCELLED.value,
            )
        return self._get_request(request_id)

    def request_view(self, request_id: int) -> RequestView:
        return self.store.read(lambda c: self._request_view(c, request_id))

    def _get_request(self, request_id: int) -> st.Request:
        request = self.store.read(lambda c: st.request_by_id(c, request_id))
        if request is None:
            raise KeyError(f"no such request: {request_id}")
        return request

    # ------------------------------------------------------------------
    # pump
    # ------------------------------------------------------------------
    def pump(self) -> PumpReport:
        """One scheduler pass: resume, reap, reclaim, evict, assign, admit, provision."""
        now = self._now()
        report = PumpReport()
        # Interrupted releases/resets first: they hold capacity until they
        # reach a terminal state, so freeing them before admission matters.
        report.resumed = self.resume_interrupted(now)
        report.reaped = self._reap_errors(now)
        report.reclaimed = self.reclaim_expired(now)
        report.evicted = self._evict_idle_for_head(now)
        report.assigned = self._assign_idle(now)
        report.admitted = self._admit_waiting(now)
        report.prewarmed = self._ensure_min(now)
        provisioned, failed = self._provision_queued(now)
        report.provisioned = provisioned
        report.failed = failed
        return report

    # -- helpers for teardown -------------------------------------------
    def _safe_stop_destroy(self, vm_ref: str) -> bool:
        """Best-effort stop+destroy; never raises.

        Returns ``True`` only when the provisioner confirms the VM is gone
        (``attach`` returns ``None`` after the destroy attempt), so callers
        can report actual removals instead of optimistic attempts.
        """
        for method in (self.provisioner.stop, self.provisioner.destroy):
            try:
                method(vm_ref)
            except Exception:  # noqa: BLE001 - teardown must not mask the failure
                log.warning("provisioner %s failed for %s", method.__name__, vm_ref, exc_info=True)
        try:
            return self.provisioner.attach(vm_ref) is None
        except Exception:  # noqa: BLE001 - cannot verify removal; assume not gone
            log.warning("could not verify removal of %s after teardown", vm_ref, exc_info=True)
            return False

    def _lease_is_dead(self, lease: st.Lease, now: datetime) -> bool:
        now_text = st.fmt_time(now)
        heartbeat_floor = st.fmt_time(
            now - timedelta(seconds=self.config.leases.heartbeat_timeout_s)
        )
        return lease.expires_at <= now_text or lease.last_heartbeat <= heartbeat_floor

    def _reservation_live(self, conn, seat: st.Seat) -> bool:
        """Is a provisioning seat still wanted (not cancelled/released)?"""
        lease = st.active_lease_for_seat(conn, seat.id)
        if lease is None:
            return st.request_for_seat(conn, seat.id) is None
        if lease.request_id is not None:
            request = st.request_by_id(conn, lease.request_id)
            if request is None or request.status in st.TERMINAL_REQUEST_STATES:
                return False
        return True

    # -- reclaim ---------------------------------------------------------
    def reclaim_expired(self, now: datetime | None = None) -> int:
        """Destroy seats whose lease died; re-check liveness before acting."""
        moment = now or self._now()
        leases = self.store.read(
            lambda c: st.expired_leases(c, moment, self.config.leases.heartbeat_timeout_s)
        )
        reclaimed = 0
        for lease in leases:
            seat = self.store.read(lambda c: st.seat_by_id(c, lease.seat_id))
            if seat is None:
                continue
            reason = (
                "lease_expired" if lease.expires_at <= st.fmt_time(moment) else "heartbeat_timeout"
            )
            with self.locks.seat(seat.name):
                outcome = self._release_seat_locked(
                    seat.id,
                    now=moment,
                    export=False,
                    repo=None,
                    request_status=st.RequestStatus.EXPIRED.value,
                    reason=reason,
                    require_dead_lease=True,
                )
            if outcome.destroyed or outcome.held:
                reclaimed += 1
        return reclaimed

    # -- error-seat reaper (FIX 3) --------------------------------------
    def _reap_errors(self, now: datetime) -> int:
        errors = self.store.read(
            lambda c: [s for s in st.list_seats(c) if s.state == st.SeatState.ERROR.value]
        )
        reaped = 0
        for seat in errors:
            with self.locks.seat(seat.name):
                with self.store.transaction() as conn:
                    fresh = st.seat_by_id(conn, seat.id)
                    if fresh is None or fresh.state != st.SeatState.ERROR.value:
                        continue
                    is_prewarm = st.request_for_seat(conn, fresh.id) is None
                    attempts = fresh.attempts
                    vm_ref = fresh.vm_name
                if vm_ref is not None:
                    self._safe_stop_destroy(vm_ref)
                acted = False
                with self.store.transaction() as conn:
                    fresh = st.seat_by_id(conn, seat.id)
                    if fresh is None or fresh.state != st.SeatState.ERROR.value:
                        continue
                    now_text = st.fmt_time(now)
                    try:
                        retry_at = st.plus_seconds(
                            st.parse_time(fresh.updated_at), self.config.prewarm.backoff_s
                        )
                    except (ValueError, TypeError):
                        # A corrupt timestamp must not abort the whole pump.
                        log.warning(
                            "seat %s has malformed updated_at %r; quarantining",
                            seat.id,
                            fresh.updated_at,
                        )
                        st.update_seat(
                            conn,
                            seat.id,
                            state=st.SeatState.OFF.value,
                            vm_name=None,
                            agent_label=None,
                            last_error="quarantined: malformed updated_at",
                            now=now_text,
                        )
                        self._close_lease(conn, seat.id, st.RequestStatus.FAILED.value, now_text)
                        st.log_event(
                            conn,
                            event_type="seat_quarantined",
                            now=now_text,
                            seat_id=seat.id,
                            detail=f"updated_at={fresh.updated_at!r}",
                        )
                        acted = True
                    else:
                        if is_prewarm and attempts > self.config.prewarm.max_retries:
                            self._prewarm_suspended.add(seat.seat_type)
                            st.update_seat(
                                conn,
                                seat.id,
                                state=st.SeatState.OFF.value,
                                vm_name=None,
                                agent_label=None,
                                last_error=f"prewarm failed after {attempts} attempt(s)",
                                now=now_text,
                            )
                            self._close_lease(
                                conn, seat.id, st.RequestStatus.FAILED.value, now_text
                            )
                            st.log_event(
                                conn,
                                event_type="seat_prewarm_suspended",
                                now=now_text,
                                seat_id=seat.id,
                                detail=f"seat_type={seat.seat_type}",
                            )
                            acted = True
                        elif is_prewarm and now >= retry_at:
                            st.update_seat(
                                conn,
                                seat.id,
                                state=st.SeatState.QUEUED.value,
                                vm_name=None,
                                last_error=None,
                                now=now_text,
                            )
                            st.log_event(
                                conn,
                                event_type="seat_prewarm_retry",
                                now=now_text,
                                seat_id=seat.id,
                                detail=f"attempt={attempts}",
                            )
                            acted = True
                        elif is_prewarm:
                            # Still inside the backoff window: keep the error
                            # row (it holds the floor) and try again later.
                            pass
                        else:
                            st.update_seat(
                                conn,
                                seat.id,
                                state=st.SeatState.OFF.value,
                                vm_name=None,
                                agent_label=None,
                                now=now_text,
                            )
                            self._close_lease(
                                conn, seat.id, st.RequestStatus.FAILED.value, now_text
                            )
                            st.log_event(
                                conn,
                                event_type="seat_reaped",
                                now=now_text,
                                seat_id=seat.id,
                            )
                            acted = True
                if acted:
                    reaped += 1
        return reaped

    # -- evict idle seats for a pinned-image head ------------------------
    def _evict_idle_for_head(self, now: datetime) -> int:
        evicted = 0
        for seat_type in self._seat_types():
            with self.store.transaction() as conn:
                request = st.next_waiting(conn, seat_type)
                if request is None:
                    continue
                wanted = self._effective_image(seat_type, request.image)
                idle = [
                    s
                    for s in st.list_seats(conn, seat_type)
                    if s.state == st.SeatState.READY.value
                    and st.active_lease_for_seat(conn, s.id) is None
                ]
                if not idle or any(s.image == wanted for s in idle):
                    continue
                victim = idle[0]
            with self.locks.seat(victim.name):
                with self.store.transaction() as conn:
                    fresh = st.seat_by_id(conn, victim.id)
                    if (
                        fresh is None
                        or fresh.state != st.SeatState.READY.value
                        or st.active_lease_for_seat(conn, fresh.id) is not None
                        or fresh.image == wanted
                    ):
                        continue
                outcome = self._release_seat_locked(
                    victim.id,
                    now=now,
                    export=False,
                    repo=None,
                    request_status=None,
                    reason="evicted_for_pinned_image",
                    require_no_lease=True,
                    require_state=st.SeatState.READY.value,
                )
            if outcome.destroyed:
                evicted += 1
        return evicted

    # -- assign idle warm seats -----------------------------------------
    def _assign_idle(self, now: datetime) -> int:
        assigned = 0
        for seat_type in self._seat_types():
            while True:
                with self.store.transaction() as conn:
                    request = st.next_waiting(conn, seat_type)
                    if request is None:
                        break
                    image = self._effective_image(seat_type, request.image)
                    seat = self._find_idle_seat(conn, seat_type, image)
                if seat is None:
                    break
                with self.locks.seat(seat.name):
                    with self.store.transaction() as conn:
                        fresh = st.seat_by_id(conn, seat.id)
                        if (
                            fresh is None
                            or fresh.state != st.SeatState.READY.value
                            or st.active_lease_for_seat(conn, fresh.id) is not None
                            or fresh.image != image
                        ):
                            break
                        st.update_request(
                            conn,
                            request.id,
                            status=st.RequestStatus.CLAIMED.value,
                            seat_id=fresh.id,
                            now=st.fmt_time(now),
                        )
                        st.create_lease(
                            conn,
                            seat_id=fresh.id,
                            request_id=request.id,
                            agent_label=request.agent_label,
                            now=st.fmt_time(now),
                            lease_timeout_s=self.config.leases.lease_timeout_s,
                        )
                        st.update_seat(
                            conn,
                            fresh.id,
                            agent_label=request.agent_label,
                            now=st.fmt_time(now),
                        )
                        st.log_event(
                            conn,
                            event_type="seat_assigned",
                            now=st.fmt_time(now),
                            seat_id=fresh.id,
                            request_id=request.id,
                            agent_label=request.agent_label,
                            detail="idle warm seat reused",
                        )
                assigned += 1
        return assigned

    def _find_idle_seat(self, conn, seat_type: str, image: str) -> st.Seat | None:
        for seat in st.list_seats(conn, seat_type):
            if seat.state != st.SeatState.READY.value:
                continue
            if seat.image != image:
                continue
            if st.active_lease_for_seat(conn, seat.id) is not None:
                continue
            return seat
        return None

    # -- admit waiting requests -----------------------------------------
    def _admit_waiting(self, now: datetime) -> int:
        admitted = 0
        for seat_type in self._seat_types():
            while True:
                with self.store.transaction() as conn:
                    if not self._admission_conn(conn, seat_type).admitted:
                        break
                    request = st.next_waiting(conn, seat_type)
                    if request is None:
                        break
                    image = self._effective_image(seat_type, request.image)
                    seat_name = self._new_seat_name(conn, seat_type)
                    seat_id = st.insert_seat(
                        conn,
                        name=seat_name,
                        seat_type=seat_type,
                        image=image,
                        state=st.SeatState.QUEUED.value,
                        agent_label=request.agent_label,
                        now=st.fmt_time(now),
                    )
                    st.update_request(
                        conn,
                        request.id,
                        status=st.RequestStatus.CLAIMED.value,
                        seat_id=seat_id,
                        now=st.fmt_time(now),
                    )
                    st.create_lease(
                        conn,
                        seat_id=seat_id,
                        request_id=request.id,
                        agent_label=request.agent_label,
                        now=st.fmt_time(now),
                        lease_timeout_s=self.config.leases.lease_timeout_s,
                    )
                    st.log_event(
                        conn,
                        event_type="seat_admitted",
                        now=st.fmt_time(now),
                        seat_id=seat_id,
                        request_id=request.id,
                        agent_label=request.agent_label,
                        detail=f"image={image}",
                    )
                admitted += 1
        return admitted

    def _new_seat_name(self, conn, seat_type: str) -> str:
        existing = {seat.name for seat in st.list_seats(conn)}
        index = 1
        while f"{seat_type}-{index}" in existing:
            index += 1
        return f"{seat_type}-{index}"

    # -- prewarm floor ---------------------------------------------------
    def _ensure_min(self, now: datetime) -> int:
        created = 0
        for seat_type in self._seat_types():
            if seat_type in self._prewarm_suspended:
                continue
            minimum = self.config.seats[seat_type].min_seats
            while True:
                with self.store.transaction() as conn:
                    floor = st.count_occupying(conn, seat_type) + st.count_seats_in_state(
                        conn, seat_type, (st.SeatState.ERROR.value,)
                    )
                    if floor >= minimum:
                        break
                    if not self._admission_conn(conn, seat_type).admitted:
                        break
                    seat_name = self._new_seat_name(conn, seat_type)
                    st.insert_seat(
                        conn,
                        name=seat_name,
                        seat_type=seat_type,
                        image=self.config.image_for(seat_type),
                        state=st.SeatState.QUEUED.value,
                        now=st.fmt_time(now),
                    )
                    st.log_event(
                        conn,
                        event_type="seat_prewarm",
                        now=st.fmt_time(now),
                        detail=f"seat_type={seat_type}",
                    )
                created += 1
        return created

    # -- provisioning ----------------------------------------------------
    def _provision_queued(self, now: datetime) -> tuple[int, int]:
        queued = self.store.read(
            lambda c: [s for s in st.list_seats(c) if s.state == st.SeatState.QUEUED.value]
        )
        provisioned = 0
        failed = 0
        for seat in queued:
            with self.locks.seat(seat.name):
                with self.store.transaction() as conn:
                    fresh = st.seat_by_id(conn, seat.id)
                    if fresh is None or fresh.state != st.SeatState.QUEUED.value:
                        continue
                    st.update_seat(
                        conn,
                        seat.id,
                        state=st.SeatState.PROVISIONING.value,
                        now=st.fmt_time(now),
                    )
                    st.log_event(
                        conn,
                        event_type="seat_provisioning",
                        now=st.fmt_time(now),
                        seat_id=seat.id,
                        agent_label=str(fresh.agent_label),
                    )
                ok, vm_ref = self._do_provision(seat)
                if not ok:
                    failed += 1
                    continue
                live = False
                with self.store.transaction() as conn:
                    fresh = st.seat_by_id(conn, seat.id)
                    live = (
                        fresh is not None
                        and fresh.state == st.SeatState.PROVISIONING.value
                        and self._reservation_live(conn, fresh)
                    )
                    if live:
                        st.update_seat(
                            conn,
                            seat.id,
                            state=st.SeatState.READY.value,
                            vm_name=vm_ref,
                            last_error=None,
                            now=st.fmt_time(now),
                        )
                        st.log_event(
                            conn,
                            event_type="seat_ready",
                            now=st.fmt_time(now),
                            seat_id=seat.id,
                            detail=f"vm={vm_ref}",
                        )
                    elif fresh is not None and fresh.state == st.SeatState.PROVISIONING.value:
                        st.update_seat(
                            conn,
                            seat.id,
                            state=st.SeatState.OFF.value,
                            vm_name=None,
                            agent_label=None,
                            now=st.fmt_time(now),
                        )
                        st.log_event(
                            conn,
                            event_type="seat_provision_orphaned",
                            now=st.fmt_time(now),
                            seat_id=seat.id,
                            detail=f"vm={vm_ref}",
                        )
                if live:
                    provisioned += 1
                elif vm_ref is not None:
                    self._safe_stop_destroy(vm_ref)
        return provisioned, failed

    def _do_provision(self, seat: st.Seat) -> tuple[bool, str | None]:
        """Create/start/wait one seat; never lets an exception escape.

        On any failure the seat becomes terminal ``error`` with the known
        ``vm_ref`` persisted, the lease is closed, the request failed, and
        the partially-created VM is best-effort destroyed.
        """
        vm_ref: str | None = None
        try:
            vm_ref = self.provisioner.create_from_image(
                seat.name, seat.seat_type, seat.image, self._resources(seat.seat_type)
            )
            self.provisioner.apply_resource_limits(vm_ref, self._resources(seat.seat_type))
            self.provisioner.start(vm_ref)
            self.provisioner.wait_ready(vm_ref, self.config.leases.lease_timeout_s)
            return True, vm_ref
        except Exception as exc:  # noqa: BLE001 - one bad seat must not kill the pump
            if vm_ref is not None:
                self._safe_stop_destroy(vm_ref)
            now = st.fmt_time(self._now())
            with self.store.transaction() as conn:
                fresh = st.seat_by_id(conn, seat.id)
                attempts = (fresh.attempts if fresh is not None else 0) + 1
                st.update_seat(
                    conn,
                    seat.id,
                    state=st.SeatState.ERROR.value,
                    vm_name=vm_ref,
                    last_error=str(exc) or exc.__class__.__name__,
                    attempts=attempts,
                    now=now,
                )
                lease = st.active_lease_for_seat(conn, seat.id)
                if lease is not None:
                    st.release_lease(conn, lease.id, now)
                    if lease.request_id is not None:
                        st.update_request(
                            conn,
                            lease.request_id,
                            status=st.RequestStatus.FAILED.value,
                            now=now,
                        )
                st.log_event(
                    conn,
                    event_type="seat_failed",
                    now=now,
                    seat_id=seat.id,
                    detail=f"{exc.__class__.__name__}: {exc}",
                )
            return False, vm_ref

    # ------------------------------------------------------------------
    # reattach / adopt (FIX 4)
    # ------------------------------------------------------------------
    def reconcile(self, vms: list[VmInfo] | None = None) -> ReconcileReport:
        """Reconcile durable seats with the provisioner's actual VMs.

        Policy for a seat whose VM is no longer running: **error and reap**
        (never silently hand out a dead VM, never restart it here — restart
        authority stays with the scheduler's normal provisioning path).
        A defined-but-not-running in-flight VM (crash between
        ``create_from_image`` and ``start``) is *destroyed during reconcile*
        and the seat is recorded ``error`` with ``vm_name`` persisted, so the
        overlay is never leaked; the reaper then closes the row out.
        ``list_vms`` returns only **managed (seat)** domains — the
        ``omavroom-base``/``omavroom-term`` templates are excluded, so they
        can never be classified as orphans. A managed VM that exists but is
        not yet persisted on a seat row (created during ``wait_ready``) is
        matched by seat-name identity and is not treated as an orphan.
        """
        if vms is None:
            vms = self.provisioner.list_vms()
        known = {vm.ref: vm for vm in vms}
        by_name = {vm.name: vm for vm in vms}
        seats = self.store.read(st.list_seats)
        referenced = {seat.vm_name for seat in seats if seat.vm_name}
        in_flight_names = {
            seat.name
            for seat in seats
            if seat.state in (st.SeatState.QUEUED.value, st.SeatState.PROVISIONING.value)
        }
        orphans = [
            vm.ref for vm in vms if vm.ref not in referenced and vm.name not in in_flight_names
        ]
        orphans_destroyed = 0
        for ref in orphans:
            if self._safe_stop_destroy(ref):
                orphans_destroyed += 1
        errored = 0
        recovered = 0
        off = 0
        for seat in seats:
            now_text = st.fmt_time(self._now())
            info = known.get(seat.vm_name) if seat.vm_name is not None else None
            resumable_release = (
                seat.state == st.SeatState.RELEASING.value
                and seat.pending_action == "release"
                and info is not None
                and info.state == "running"
            )
            resumable_reset = seat.state == st.SeatState.RESETTING.value and info is not None
            if resumable_release or resumable_reset:
                # Interrupted release/reset with a **present, running** VM:
                # resumed after the scan by ``resume_interrupted`` (idempotent
                # export/destroy or overlay rebuild) so it reaches a terminal
                # state instead of wedging the seat in an occupying state.
                # Absent/stopped VMs are NOT skipped: a gone or stopped VM
                # proves the export already finished (export precedes
                # stop/destroy), so the row is finalized to ``off`` below.
                continue
            if seat.vm_name is None:
                if seat.state == st.SeatState.PROVISIONING.value:
                    info = by_name.get(seat.name)
                    if info is not None and info.state == "running":
                        # In-flight VM created during wait_ready but not yet
                        # persisted: adopt it instead of erroring/reaping.
                        with self.store.transaction() as conn:
                            st.update_seat(
                                conn,
                                seat.id,
                                state=st.SeatState.READY.value,
                                vm_name=info.ref,
                                now=now_text,
                            )
                            st.log_event(
                                conn,
                                event_type="seat_reconcile_recovered",
                                now=now_text,
                                seat_id=seat.id,
                                detail=f"in-flight vm={info.ref}",
                            )
                        recovered += 1
                    else:
                        # Crash between create_from_image and start: the domain
                        # is defined but not running. Destroy it here so the
                        # overlay cannot leak, persist its ref for audit, and
                        # let the reaper close the row out.
                        if info is not None:
                            self._safe_stop_destroy(info.ref)
                        with self.store.transaction() as conn:
                            st.update_seat(
                                conn,
                                seat.id,
                                state=st.SeatState.ERROR.value,
                                vm_name=info.ref if info is not None else None,
                                last_error="reconcile: seat had no running VM at provisioning",
                                now=now_text,
                            )
                            self._close_lease(
                                conn, seat.id, st.RequestStatus.FAILED.value, now_text
                            )
                            st.log_event(
                                conn,
                                event_type="seat_reconcile_error",
                                now=now_text,
                                seat_id=seat.id,
                                detail=f"destroyed defined vm={info.ref}"
                                if info is not None
                                else None,
                            )
                        errored += 1
                elif seat.state in (
                    st.SeatState.RELEASING.value,
                    st.SeatState.HELD.value,
                    st.SeatState.RESETTING.value,
                ):
                    # No VM to tear down or reset: close the row out so a
                    # half-committed release/reset can never occupy capacity.
                    with self.store.transaction() as conn:
                        st.update_seat(
                            conn,
                            seat.id,
                            state=st.SeatState.OFF.value,
                            vm_name=None,
                            agent_label=None,
                            pending_action=None,
                            pending_export=None,
                            pending_repo=None,
                            pending_branch=None,
                            pending_ref=None,
                            pending_request_status=None,
                            now=now_text,
                        )
                        self._close_lease(conn, seat.id, st.RequestStatus.FAILED.value, now_text)
                        st.log_event(
                            conn,
                            event_type="seat_reconcile_off",
                            now=now_text,
                            seat_id=seat.id,
                        )
                    off += 1
                continue
            info = known.get(seat.vm_name)
            if info is None:
                if seat.state in (
                    st.SeatState.READY.value,
                    st.SeatState.BUSY.value,
                    st.SeatState.RESETTING.value,
                    st.SeatState.PROVISIONING.value,
                ):
                    with self.store.transaction() as conn:
                        st.update_seat(
                            conn,
                            seat.id,
                            state=st.SeatState.ERROR.value,
                            last_error=f"reconcile: VM {seat.vm_name} is unknown",
                            now=now_text,
                        )
                        self._close_lease(conn, seat.id, st.RequestStatus.FAILED.value, now_text)
                        st.log_event(
                            conn,
                            event_type="seat_reconcile_error",
                            now=now_text,
                            seat_id=seat.id,
                            detail=f"unknown vm={seat.vm_name}",
                        )
                    errored += 1
                elif seat.state in (
                    st.SeatState.RELEASING.value,
                    st.SeatState.HELD.value,
                ):
                    with self.store.transaction() as conn:
                        st.update_seat(
                            conn,
                            seat.id,
                            state=st.SeatState.OFF.value,
                            vm_name=None,
                            agent_label=None,
                            pending_action=None,
                            pending_export=None,
                            pending_repo=None,
                            pending_branch=None,
                            pending_ref=None,
                            pending_request_status=None,
                            now=now_text,
                        )
                        self._close_lease(conn, seat.id, st.RequestStatus.FAILED.value, now_text)
                        st.log_event(
                            conn,
                            event_type="seat_reconcile_off",
                            now=now_text,
                            seat_id=seat.id,
                        )
                    off += 1
            elif seat.state == st.SeatState.PROVISIONING.value and info.state == "running":
                with self.store.transaction() as conn:
                    st.update_seat(
                        conn,
                        seat.id,
                        state=st.SeatState.READY.value,
                        now=now_text,
                    )
                    st.log_event(
                        conn,
                        event_type="seat_reconcile_recovered",
                        now=now_text,
                        seat_id=seat.id,
                        detail=f"vm={seat.vm_name}",
                    )
                recovered += 1
            elif (
                seat.state
                in (
                    st.SeatState.READY.value,
                    st.SeatState.BUSY.value,
                    st.SeatState.RESETTING.value,
                    st.SeatState.PROVISIONING.value,
                )
                and info.state != "running"
            ):
                with self.store.transaction() as conn:
                    st.update_seat(
                        conn,
                        seat.id,
                        state=st.SeatState.ERROR.value,
                        last_error=(
                            f"reconcile: VM {seat.vm_name} is not running (state={info.state})"
                        ),
                        now=now_text,
                    )
                    self._close_lease(conn, seat.id, st.RequestStatus.FAILED.value, now_text)
                    st.log_event(
                        conn,
                        event_type="seat_reconcile_dead",
                        now=now_text,
                        seat_id=seat.id,
                        detail=f"vm={seat.vm_name} state={info.state}",
                    )
                errored += 1
        # Now that VM-absent and dead cases are resolved, resume any
        # interrupted release/reset that still has a live VM.
        resumed = self.resume_interrupted(now=self._now(), vms=known)
        return ReconcileReport(
            orphans_destroyed=orphans_destroyed,
            seats_errored=errored,
            seats_recovered=recovered,
            seats_off=off,
            seats_resumed=resumed,
        )

    # ------------------------------------------------------------------
    # lease / work operations
    # ------------------------------------------------------------------
    def heartbeat(
        self,
        *,
        seat_id: int | None = None,
        request_id: int | None = None,
        lease_id: int | None = None,
    ) -> LeaseView:
        """Renew a lease's liveness on the independent heartbeat channel."""
        now = st.fmt_time(self._now())
        with self.store.transaction() as conn:
            lease = None
            if lease_id is not None:
                lease = st.lease_by_id(conn, lease_id)
            elif seat_id is not None:
                lease = st.active_lease_for_seat(conn, seat_id)
            elif request_id is not None:
                lease = st.lease_for_request(conn, request_id)
            if lease is None:
                raise KeyError("no active lease for heartbeat")
            st.touch_heartbeat(conn, lease.id, now)
            st.log_event(
                conn,
                event_type="heartbeat",
                now=now,
                seat_id=lease.seat_id,
                request_id=lease.request_id,
                agent_label=lease.agent_label,
            )
            lease = st.lease_by_id(conn, lease.id)
        return self._lease_view(lease)

    def begin_work(self, seat_id: int) -> SeatView:
        """Mark a leased ready seat busy (first exec started)."""
        now = st.fmt_time(self._now())
        with self.store.transaction() as conn:
            seat = st.seat_by_id(conn, seat_id)
            if seat is None:
                raise KeyError(f"no such seat: {seat_id}")
            if seat.state == st.SeatState.READY.value:
                st.update_seat(conn, seat_id, state=st.SeatState.BUSY.value, now=now)
                st.log_event(conn, event_type="seat_busy", now=now, seat_id=seat_id)
            seat = st.seat_by_id(conn, seat_id)
        return self._seat_view(seat)

    def finish_work(self, seat_id: int) -> SeatView:
        """Return a busy seat to ready (all execs finished; lease retained)."""
        now = st.fmt_time(self._now())
        with self.store.transaction() as conn:
            seat = st.seat_by_id(conn, seat_id)
            if seat is None:
                raise KeyError(f"no such seat: {seat_id}")
            if seat.state == st.SeatState.BUSY.value:
                st.update_seat(conn, seat_id, state=st.SeatState.READY.value, now=now)
                st.log_event(conn, event_type="seat_idle", now=now, seat_id=seat_id)
            seat = st.seat_by_id(conn, seat_id)
        return self._seat_view(seat)

    # ------------------------------------------------------------------
    # reset / export / release
    # ------------------------------------------------------------------
    def reset_seat(self, seat_id: int) -> SeatView:
        """Revert a seat to clean without releasing (export/reset audit delta).

        The final ``ready`` write is revalidated inside the transaction using
        the same reservation-liveness check as provisioning: if the seat was
        released/cancelled while the slow reset ran, it is not resurrected —
        the correct terminal state is written and the VM is discarded.
        """
        seat = self._get_seat(seat_id)
        with self.locks.seat(seat.name):
            now = st.fmt_time(self._now())
            with self.store.transaction() as conn:
                fresh = st.seat_by_id(conn, seat_id)
                if fresh is None or fresh.vm_name is None:
                    raise ProvisionerError("cannot reset a seat with no VM")
                st.update_seat(
                    conn,
                    seat_id,
                    state=st.SeatState.RESETTING.value,
                    pending_action="reset",
                    now=now,
                )
                st.log_event(conn, event_type="seat_resetting", now=now, seat_id=seat_id)
            vm_ref = fresh.vm_name
            try:
                self.provisioner.reset(vm_ref)
            except Exception as exc:  # noqa: BLE001 - reset failure is terminal for the seat
                now = st.fmt_time(self._now())
                with self.store.transaction() as conn:
                    st.update_seat(
                        conn,
                        seat_id,
                        state=st.SeatState.ERROR.value,
                        last_error=str(exc),
                        pending_action=None,
                        now=now,
                    )
                    self._close_lease(conn, seat_id, st.RequestStatus.FAILED.value, now)
                    st.log_event(
                        conn,
                        event_type="seat_reset_failed",
                        now=now,
                        seat_id=seat_id,
                        detail=str(exc),
                    )
                raise
            now = st.fmt_time(self._now())
            live = False
            with self.store.transaction() as conn:
                fresh = st.seat_by_id(conn, seat_id)
                live = (
                    fresh is not None
                    and fresh.state == st.SeatState.RESETTING.value
                    and self._reservation_live(conn, fresh)
                )
                if live:
                    st.update_seat(
                        conn,
                        seat_id,
                        state=st.SeatState.READY.value,
                        last_error=None,
                        pending_action=None,
                        now=now,
                    )
                    st.log_event(conn, event_type="seat_reset", now=now, seat_id=seat_id)
                elif fresh is not None and fresh.state == st.SeatState.RESETTING.value:
                    st.update_seat(
                        conn,
                        seat_id,
                        state=st.SeatState.OFF.value,
                        vm_name=None,
                        agent_label=None,
                        pending_action=None,
                        now=now,
                    )
                    self._close_lease(conn, seat_id, None, now)
                    st.log_event(
                        conn,
                        event_type="seat_reset_orphaned",
                        now=now,
                        seat_id=seat_id,
                    )
                ready = st.seat_by_id(conn, seat_id) or fresh
        if not live:
            self._safe_stop_destroy(vm_ref)
        return self._seat_view(ready)

    def _export_locked(self, seat: st.Seat, spec: ExportSpec) -> ExportOutcome:
        """fetch -> gate -> push under the seat (+repo) lock; never destroys.

        Exception-safe: a raise from ``fetch_bundle``, the gate/approver, or
        ``push`` becomes a failed :class:`ExportOutcome` (never propagates),
        so the caller can hold the seat instead of leaving it ``releasing``.
        """
        if seat.vm_name is None:
            return ExportOutcome(fetched=FetchResult(ok=False, message="seat has no VM"))
        try:
            fetched = self.provisioner.fetch_bundle(seat.vm_name, spec)
        except Exception as exc:  # noqa: BLE001 - export failure holds the seat
            log.warning("fetch_bundle failed for seat %s", seat.id, exc_info=True)
            return ExportOutcome(fetched=FetchResult(ok=False, message=f"fetch failed: {exc}"))
        try:
            decision = self.gate.evaluate(fetched)
        except Exception as exc:  # noqa: BLE001 - a broken gate must not wedge
            log.warning("export gate raised for seat %s", seat.id, exc_info=True)
            return ExportOutcome(fetched=fetched, gate=GateDecision(False, f"gate error: {exc}"))
        if fetched.ok and fetched.sha is None:
            # Contract: a successful fetch must carry a non-null SHA. Without
            # one we cannot verify the push, so hold rather than accept it.
            return ExportOutcome(
                fetched=fetched,
                gate=decision,
                pushed=PushResult(ok=False, message="fetch returned no SHA; refusing to push"),
            )
        pushed: PushResult | None = None
        if fetched.ok and decision.allowed:
            try:
                pushed = self.provisioner.push(spec, fetched)
            except Exception as exc:  # noqa: BLE001 - push failure holds the seat
                log.warning("push failed for seat %s", seat.id, exc_info=True)
                return ExportOutcome(
                    fetched=fetched,
                    gate=decision,
                    pushed=PushResult(ok=False, message=f"push failed: {exc}"),
                )
            if pushed.ok and fetched.sha is not None and pushed.sha != fetched.sha:
                pushed = PushResult(
                    ok=False,
                    sha=pushed.sha,
                    message=f"pushed SHA {pushed.sha} != fetched SHA {fetched.sha}",
                )
        return ExportOutcome(fetched=fetched, gate=decision, pushed=pushed)

    def export_seat(
        self,
        seat_id: int,
        *,
        repo: str,
        branch: str | None = None,
        ref: str | None = None,
    ) -> ExportOutcome:
        """Export work under the seat-then-repo lock order (no teardown).

        ``branch``/``ref`` are plumbed into the :class:`ExportSpec` so the
        host-side push knows the task branch and explicit destination; without
        them a manager-mediated export cannot resolve a push target. Failure
        never mutates the seat: it stays ``ready`` (no push, no destroy) so
        the caller can retry or release later.
        """
        seat = self._get_seat(seat_id)
        if seat.vm_name is None:
            return ExportOutcome(fetched=FetchResult(ok=False, message="seat has no VM to export"))
        with self.locks.seat_then_repo(seat.name, repo):
            fresh = self._get_seat(seat_id)
            if fresh.vm_name is None:
                return ExportOutcome(
                    fetched=FetchResult(ok=False, message="seat has no VM to export")
                )
            return self._export_locked(fresh, ExportSpec(repo=repo, branch=branch, ref=ref))

    def release_seat(
        self,
        seat_id: int,
        *,
        repo: str | None = None,
        export: bool = True,
        branch: str | None = None,
        ref: str | None = None,
        request_status: str = st.RequestStatus.DONE.value,
    ) -> ReleaseOutcome:
        """Export (optional) then destroy the seat's VM. Never hands it on dirty.

        ``branch``/``ref`` are plumbed straight into the export spec so a
        manager-mediated release-with-export reaches the same verified
        destination as a direct provisioner call.
        """
        seat = self._get_seat(seat_id)
        repo_key = repo if (export and repo is not None) else None
        with self.locks.seat_then_repo(seat.name, repo_key):
            return self._release_seat_locked(
                seat_id,
                now=self._now(),
                export=export,
                repo=repo,
                branch=branch,
                ref=ref,
                request_status=request_status,
            )

    def _release_seat_locked(
        self,
        seat_id: int,
        *,
        now: datetime,
        export: bool,
        repo: str | None,
        request_status: str | None,
        branch: str | None = None,
        ref: str | None = None,
        reason: str = "released",
        require_dead_lease: bool = False,
        require_no_lease: bool = False,
        require_state: str | None = None,
    ) -> ReleaseOutcome:
        now_text = st.fmt_time(now)
        with self.store.transaction() as conn:
            seat = st.seat_by_id(conn, seat_id)
            if seat is None:
                return ReleaseOutcome(seat_id, destroyed=False, held=False, message="no seat")
            if require_state is not None and seat.state != require_state:
                return ReleaseOutcome(
                    seat_id, destroyed=False, held=False, message=f"state is {seat.state}"
                )
            if seat.state == st.SeatState.OFF.value:
                self._close_lease(conn, seat_id, request_status, now_text)
                return ReleaseOutcome(seat_id, destroyed=False, held=False, message="already off")
            if require_dead_lease:
                lease = st.active_lease_for_seat(conn, seat_id)
                if lease is None:
                    return ReleaseOutcome(
                        seat_id, destroyed=False, held=False, message="no active lease"
                    )
                if not self._lease_is_dead(lease, now):
                    return ReleaseOutcome(
                        seat_id, destroyed=False, held=False, message="lease_still_live"
                    )
            if require_no_lease and st.active_lease_for_seat(conn, seat_id) is not None:
                return ReleaseOutcome(
                    seat_id, destroyed=False, held=False, message="seat_now_leased"
                )
            st.update_seat(
                conn,
                seat_id,
                state=st.SeatState.RELEASING.value,
                pending_action="release",
                pending_export=1 if export else 0,
                pending_repo=repo,
                pending_branch=branch,
                pending_ref=ref,
                pending_request_status=request_status,
                now=now_text,
            )
            st.log_event(conn, event_type="seat_releasing", now=now_text, seat_id=seat_id)

        export_outcome: ExportOutcome | None = None
        if export and repo is not None and seat.vm_name is not None:
            export_outcome = self._export_locked(
                seat, ExportSpec(repo=repo, branch=branch, ref=ref)
            )
            if not export_outcome.ok:
                with self.store.transaction() as conn:
                    st.update_seat(
                        conn,
                        seat_id,
                        state=st.SeatState.HELD.value,
                        last_error=export_outcome.message,
                        now=now_text,
                    )
                    # A failed export is not completion: the request failed and
                    # the VM is held for recovery, never pushed or destroyed.
                    self._close_lease(conn, seat_id, st.RequestStatus.FAILED.value, now_text)
                    st.log_event(
                        conn,
                        event_type="seat_held",
                        now=now_text,
                        seat_id=seat_id,
                        detail=export_outcome.message,
                    )
                return ReleaseOutcome(
                    seat_id,
                    destroyed=False,
                    held=True,
                    export=export_outcome,
                    message=export_outcome.message,
                )

        removed = True
        if seat.vm_name is not None:
            removed = self._safe_stop_destroy(seat.vm_name)

        with self.store.transaction() as conn:
            st.update_seat(
                conn,
                seat_id,
                state=st.SeatState.OFF.value,
                vm_name=None,
                agent_label=None,
                last_error=None,
                pending_action=None,
                pending_export=None,
                pending_repo=None,
                pending_branch=None,
                pending_ref=None,
                pending_request_status=None,
                now=now_text,
            )
            self._close_lease(conn, seat_id, request_status, now_text)
            st.log_event(
                conn,
                event_type="seat_released",
                now=now_text,
                seat_id=seat_id,
                detail=f"reason={reason}",
            )
        return ReleaseOutcome(
            seat_id, destroyed=removed, held=False, export=export_outcome, message=reason
        )

    def _close_lease(self, conn, seat_id: int, request_status: str | None, now_text: str) -> None:
        lease = st.active_lease_for_seat(conn, seat_id)
        if lease is None:
            return
        st.release_lease(conn, lease.id, now_text)
        if lease.request_id is not None and request_status is not None:
            st.update_request(
                conn,
                lease.request_id,
                status=request_status,
                now=now_text,
            )

    # ------------------------------------------------------------------
    # interrupted-operation recovery / operator escape hatches
    # ------------------------------------------------------------------
    def resume_interrupted(
        self, now: datetime | None = None, vms: dict[str, VmInfo] | None = None
    ) -> int:
        """Resume releases/resets interrupted by a daemon restart.

        A release persists its export spec (repo/branch/ref/export) before the
        slow ``fetch -> gate -> push`` work begins; re-running the export is
        only safe while the VM is **present and running**, because a gone or
        stopped VM proves the export already finished (export always precedes
        stop/destroy) and re-exporting would falsely land the seat ``held``.
        Such a seat is finalized to ``off`` instead. Reset is spec-free and is
        resumed whenever the VM is present; a failed resume falls back to
        discard. Not idempotent in general: a release whose VM vanished between
        the attach probe and the export is held, never destroyed.

        Called under the tick lock from :meth:`reconcile` and :meth:`pump`, so
        it can never overlap an in-flight release. Returns the number of seats
        returned to a terminal state.
        """
        moment = now or self._now()
        seats = self.store.read(st.list_seats)
        resumed = 0
        for seat in seats:
            if seat.state == st.SeatState.RELEASING.value and seat.pending_action == "release":
                vm_state = self._vm_state_for(seat, vms)
                if vm_state is None or vm_state not in ("running", "unknown"):
                    # Absent or a known non-running state: export already
                    # completed. ``"unknown"`` (attach failed) is treated as
                    # present so a transient error cannot discard work.
                    if self._finalize_gone_release(seat, moment, reason="reconcile_resume"):
                        resumed += 1
                    continue
                repo = seat.pending_repo
                repo_key = repo if (seat.pending_export and repo) else None
                with self.locks.seat_then_repo(seat.name, repo_key):
                    outcome = self._release_seat_locked(
                        seat.id,
                        now=moment,
                        export=bool(seat.pending_export),
                        repo=repo,
                        branch=seat.pending_branch,
                        ref=seat.pending_ref,
                        request_status=seat.pending_request_status,
                        reason="reconcile_resume",
                    )
                if outcome.destroyed or outcome.held:
                    resumed += 1
            elif seat.state == st.SeatState.RESETTING.value and seat.vm_name is not None:
                if self._resume_reset(seat):
                    resumed += 1
        return resumed

    def _vm_state_for(self, seat: st.Seat, vms: dict[str, VmInfo] | None) -> str | None:
        """Return a seat VM's state, ``None`` if it is gone, or ``"unknown"``.

        ``"unknown"`` (attach failed) is treated as *present* by callers so a
        transient control-plane error can never silently discard work.
        """
        if seat.vm_name is None:
            return None
        if vms is not None:
            info = vms.get(seat.vm_name)
            return info.state if info is not None else None
        try:
            info = self.provisioner.attach(seat.vm_name)
        except Exception:  # noqa: BLE001 - unknown, not absent
            log.warning("could not attach %s during resume", seat.vm_name, exc_info=True)
            return "unknown"
        return info.state if info is not None else None

    def _finalize_gone_release(self, seat: st.Seat, now: datetime, *, reason: str) -> bool:
        """Close a pending release whose VM is already gone/stopped to ``off``.

        Export always precedes stop/destroy, so a vanished/stopped VM is proof
        the export finished. Re-running it would only fail and wrongly hold the
        seat. The persisted request status is used so a cancel/expire is not
        rewritten as success. A stopped-but-defined VM is best-effort destroyed
        so it cannot leak; an absent VM is a no-op.
        """
        now_text = st.fmt_time(now)
        with self.locks.seat(seat.name):
            with self.store.transaction() as conn:
                fresh = st.seat_by_id(conn, seat.id)
                if fresh is None or fresh.state != st.SeatState.RELEASING.value:
                    return False
                vm_ref = fresh.vm_name
                st.update_seat(
                    conn,
                    seat.id,
                    state=st.SeatState.OFF.value,
                    vm_name=None,
                    agent_label=None,
                    last_error=None,
                    pending_action=None,
                    pending_export=None,
                    pending_repo=None,
                    pending_branch=None,
                    pending_ref=None,
                    pending_request_status=None,
                    now=now_text,
                )
                self._close_lease(conn, seat.id, fresh.pending_request_status, now_text)
                st.log_event(
                    conn,
                    event_type="seat_reconcile_off",
                    now=now_text,
                    seat_id=seat.id,
                    detail=reason,
                )
            # A stopped-but-defined VM is not leaked; an absent one is a no-op.
            if vm_ref is not None:
                self._safe_stop_destroy(vm_ref)
        return True

    def _resume_reset(self, seat: st.Seat) -> bool:
        """Complete an interrupted reset, falling back to discard on failure.

        Reset has no parameters to lose, so a ``resetting`` seat with a live
        VM is always recoverable: re-running :meth:`reset_seat` rebuilds the
        overlay. If that fails we destroy the seat rather than leave it
        wedged in ``resetting`` (which occupies capacity).
        """
        try:
            self.reset_seat(seat.id)
            return True
        except Exception:  # noqa: BLE001 - fall back to discard, never wedge
            log.warning("reset resume failed for seat %s; force-discarding", seat.id, exc_info=True)
        try:
            self.force_discard(seat.id, reason="reset_resume_failed")
            return True
        except Exception:  # noqa: BLE001 - last resort; keep the seat surfaced
            log.warning("force discard after failed reset resume also failed", exc_info=True)
            return False

    def retry_release(self, seat_id: int) -> ReleaseOutcome:
        """Operator action: re-run the persisted release intent for a seat.

        Works for a seat left ``releasing``/``held`` by an interrupted or
        failed export. Raises :class:`ProvisionerError` when no release intent
        is recorded, so callers must use :meth:`force_discard` instead.
        """
        seat = self._get_seat(seat_id)
        if seat.pending_action != "release":
            raise ProvisionerError(f"seat {seat_id} has no persisted release intent to retry")
        repo = seat.pending_repo
        repo_key = repo if (seat.pending_export and repo) else None
        with self.locks.seat_then_repo(seat.name, repo_key):
            return self._release_seat_locked(
                seat_id,
                now=self._now(),
                export=bool(seat.pending_export),
                repo=repo,
                branch=seat.pending_branch,
                ref=seat.pending_ref,
                request_status=seat.pending_request_status,
                reason="retry_release",
            )

    def force_discard(self, seat_id: int, *, reason: str = "force_discard") -> SeatView:
        """Operator escape hatch: destroy the VM and release the seat row.

        This is the guaranteed way to unblock the pool for a seat stuck in
        ``releasing``/``held``/``resetting`` (e.g. when release intent could
        not be recovered). It never exports, it only destroys.
        """
        seat = self._get_seat(seat_id)
        with self.locks.seat(seat.name):
            now_text = st.fmt_time(self._now())
            with self.store.transaction() as conn:
                fresh = st.seat_by_id(conn, seat_id)
                if fresh is None:
                    raise KeyError(f"no such seat: {seat_id}")
                vm_ref = fresh.vm_name
                st.update_seat(conn, seat_id, state=st.SeatState.RELEASING.value, now=now_text)
            if vm_ref is not None:
                self._safe_stop_destroy(vm_ref)
            with self.store.transaction() as conn:
                st.update_seat(
                    conn,
                    seat_id,
                    state=st.SeatState.OFF.value,
                    vm_name=None,
                    agent_label=None,
                    last_error=None,
                    pending_action=None,
                    pending_export=None,
                    pending_repo=None,
                    pending_branch=None,
                    pending_ref=None,
                    pending_request_status=None,
                    now=now_text,
                )
                self._close_lease(conn, seat_id, st.RequestStatus.FAILED.value, now_text)
                st.log_event(
                    conn,
                    event_type="seat_force_discarded",
                    now=now_text,
                    seat_id=seat_id,
                    detail=reason,
                )
                fresh = st.seat_by_id(conn, seat_id)
        return self._seat_view(fresh)

    def attention_seat_ids(self, seats: list[st.Seat] | None = None) -> list[int]:
        """Seat ids an operator may need to act on (see :class:`PoolStatus`)."""
        rows = seats if seats is not None else self.store.read(st.list_seats)
        return sorted(
            seat.id
            for seat in rows
            if seat.state == st.SeatState.HELD.value
            or (
                seat.state in (st.SeatState.RELEASING.value, st.SeatState.RESETTING.value)
                and seat.pending_action is None
                and seat.vm_name is not None
            )
        )

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    def _lease_view(self, lease: st.Lease) -> LeaseView:
        return LeaseView(
            id=lease.id,
            seat_id=lease.seat_id,
            request_id=lease.request_id,
            agent_label=lease.agent_label,
            acquired_at=lease.acquired_at,
            expires_at=lease.expires_at,
            last_heartbeat=lease.last_heartbeat,
        )

    @staticmethod
    def _needs_attention(seat: st.Seat) -> bool:
        return seat.state == st.SeatState.HELD.value or (
            seat.state in (st.SeatState.RELEASING.value, st.SeatState.RESETTING.value)
            and seat.pending_action is None
            and seat.vm_name is not None
        )

    def _seat_view(self, seat: st.Seat) -> SeatView:
        lease = self.store.read(lambda c: st.active_lease_for_seat(c, seat.id))
        return SeatView(
            id=seat.id,
            name=seat.name,
            seat_type=seat.seat_type,
            state=seat.state,
            vm_name=seat.vm_name,
            image=seat.image,
            agent_label=seat.agent_label,
            last_error=seat.last_error,
            attempts=seat.attempts,
            lease_expires_at=lease.expires_at if lease else None,
            last_heartbeat=lease.last_heartbeat if lease else None,
            pending_action=seat.pending_action,
            needs_attention=self._needs_attention(seat),
        )

    def _request_view(self, conn, request_id: int) -> RequestView:
        request = st.request_by_id(conn, request_id)
        if request is None:
            raise KeyError(f"no such request: {request_id}")
        seat = st.seat_by_id(conn, request.seat_id) if request.seat_id else None
        lease = (
            st.lease_for_request(conn, request.id)
            if request.status == st.RequestStatus.CLAIMED.value
            else None
        )
        queue_ahead = (
            st.count_waiting_ahead(conn, request)
            if request.status == st.RequestStatus.WAITING.value
            else 0
        )
        seat_view = None
        if seat is not None:
            seat_view = SeatView(
                id=seat.id,
                name=seat.name,
                seat_type=seat.seat_type,
                state=seat.state,
                vm_name=seat.vm_name,
                image=seat.image,
                agent_label=seat.agent_label,
                last_error=seat.last_error,
                attempts=seat.attempts,
                lease_expires_at=lease.expires_at if lease else None,
                last_heartbeat=lease.last_heartbeat if lease else None,
                pending_action=seat.pending_action,
                needs_attention=self._needs_attention(seat),
            )
        return RequestView(
            id=request.id,
            agent_label=request.agent_label,
            seat_type=request.seat_type,
            project=request.project,
            image=self._effective_image(request.seat_type, request.image),
            status=request.status,
            position=request.position,
            seat_id=request.seat_id,
            created_at=request.created_at,
            updated_at=request.updated_at,
            queue_ahead=queue_ahead,
            seat=seat_view,
            lease=self._lease_view(lease) if lease else None,
        )

    def list_seats(self, *, include_history: bool = False) -> list[SeatView]:
        """Active seats by default; ``include_history`` adds ``off`` rows."""
        seats = self.store.read(st.list_seats)
        if not include_history:
            seats = [s for s in seats if s.state != st.SeatState.OFF.value]
        return [self._seat_view(seat) for seat in seats]

    def queue_view(self, *, include_history: bool = False) -> list[RequestView]:
        """Waiting/claimed requests by default; ``include_history`` adds finals."""
        conn = self.store.connect()
        try:
            if include_history:
                requests = st.list_requests(conn)
            else:
                requests = st.list_requests(
                    conn,
                    statuses=(
                        st.RequestStatus.WAITING.value,
                        st.RequestStatus.CLAIMED.value,
                    ),
                )
            return [self._request_view(conn, r.id) for r in requests]
        finally:
            conn.close()

    def list_events(self, *, limit: int = 200) -> list[st.Event]:
        """Most recent events, bounded by default (``limit=None`` for all)."""
        return self.store.read(lambda c: st.list_events(c, limit=limit))

    def pool_status(self) -> PoolStatus:
        free_mb = self._free_ram_mb()
        seats = self.list_seats()
        queue = self.queue_view()
        all_seats = self.store.read(st.list_seats)
        per_type = {}
        for seat_type in self._seat_types():
            occupying = sum(
                1 for s in seats if s.seat_type == seat_type and s.state in st.OCCUPYING_SEAT_STATES
            )
            error_seats = sum(
                1
                for s in all_seats
                if s.seat_type == seat_type and s.state == st.SeatState.ERROR.value
            )
            waiting = sum(
                1
                for r in queue
                if r.seat_type == seat_type and r.status == st.RequestStatus.WAITING.value
            )
            decision = self.admission(seat_type)
            cfg = self.config.seats[seat_type]
            per_type[seat_type] = TypeStatus(
                seat_type=seat_type,
                occupying=occupying,
                error_seats=error_seats,
                max_seats=cfg.max_seats,
                min_seats=cfg.min_seats,
                waiting=waiting,
                admitted=decision.admitted,
                reason=decision.reason,
            )
        return PoolStatus(
            free_ram_mb=free_mb,
            headroom_floor_mb=self.config.host.headroom_floor_mb,
            admission_override=self.override,
            seats=seats,
            queue=queue,
            per_type=per_type,
            needs_attention=self.attention_seat_ids(all_seats),
        )


# Re-exported for callers wiring exports (Phase 4B/5 use this).
__all__ = [
    "AdmissionDecision",
    "ExportGate",
    "LeaseView",
    "LockManager",
    "PoolStatus",
    "PumpReport",
    "ReconcileReport",
    "ReleaseOutcome",
    "RequestView",
    "Scheduler",
    "SeatView",
    "TypeStatus",
    "read_free_ram_mb",
]
