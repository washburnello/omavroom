"""Phase 4B2 gate fixes: interrupted release/reset recovery and operator escape
hatches (FIX 1), plus the JobRegistry bound (FIX 4, unit part).

The reproducibility trick here is the same the Tester used: a "gated" fake
blocks inside the slow operation after the seat's intent has been committed,
then we abandon that manager (simulating SIGKILL) and reconcile a fresh manager
against the same sqlite file with a fake that still reports the VM. Pre-fix the
``releasing``/``resetting`` row with a live VM wedged forever.
"""

from __future__ import annotations

import threading
import time

import pytest

from omavroom import state as st
from omavroom.config import Config
from omavroom.manager import Manager
from omavroom.manager.provisioner import (
    ExportSpec,
    FakeProvisioner,
    ProvisionerError,
    ResourceCaps,
)


def _config(*, max_seats: int = 1) -> Config:
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    cfg.seats["terminal"].max_seats = max_seats
    cfg.seats["terminal"].min_seats = 0
    return cfg


def _row(manager: Manager, seat_id: int) -> st.Seat:
    return manager.store.read(lambda c: st.seat_by_id(c, seat_id))


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class GatedFake(FakeProvisioner):
    """FakeProvisioner whose fetch/reset blocks after the intent is durable."""

    def __init__(self, *, gate: threading.Event, op: str = "fetch", **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate = gate
        self.op = op
        self.started = threading.Event()

    def fetch_bundle(self, vm_ref, export):  # type: ignore[override]
        if self.op == "fetch":
            self.started.set()
            self.gate.wait()
        return super().fetch_bundle(vm_ref, export)

    def reset(self, vm_ref):  # type: ignore[override]
        if self.op == "reset":
            self.started.set()
            self.gate.wait()
        return super().reset(vm_ref)


def _manager(tmp_path, cfg, fake, clock) -> Manager:
    return Manager(
        cfg,
        db_path=tmp_path / "state.db",
        provisioner=fake,
        clock=clock,
        free_ram_mb=lambda: 10**9,
        tick_s=0.01,
    )


def _ready_seat(manager: Manager, label: str = "A"):
    handle = manager.request_seat(label, "terminal")
    view = handle.wait_ready(timeout=5)
    return view.seat


# --------------------------------------------------------------------------
# FIX 1 - interrupted release resumes to a terminal state
# --------------------------------------------------------------------------
def test_interrupted_release_resumes_on_reconcile(tmp_path, clock):
    cfg = _config()
    gate = threading.Event()
    fake1 = GatedFake(gate=gate, op="fetch")
    mgr1 = _manager(tmp_path, cfg, fake1, clock)
    mgr1.start()
    seat = _ready_seat(mgr1)

    mgr1.release_seat(seat.id, repo="demo", branch="task", ref="origin:refs/heads/task")
    assert fake1.started.wait(5), "release never reached the export step"
    assert _wait_until(
        lambda: (
            _row(mgr1, seat.id).state == "releasing"
            and _row(mgr1, seat.id).pending_action == "release"
        )
    ), "release intent was not persisted before the slow export"
    # Simulate SIGKILL: abandon mgr1 with its worker blocked on the gate.

    fake2 = FakeProvisioner()
    fake2.inject_vm(seat.name, seat_type="terminal", state="running")
    mgr2 = _manager(tmp_path, cfg, fake2, clock)
    report = mgr2.reconcile()

    assert report.seats_resumed >= 1
    row = _row(mgr2, seat.id)
    assert row.state == "off"
    assert row.vm_name is None
    assert row.pending_action is None
    assert fake2.destroyed == [f"fake://{seat.name}"]

    # Capacity is free again: the next request is admitted.
    handle = mgr2.request_seat("B", "terminal")
    assert handle.wait_ready(timeout=5).seat.state == "ready"


def test_interrupted_release_with_export_failure_lands_held(tmp_path, clock):
    """A resumed release whose export fails is held (never stuck releasing)."""
    cfg = _config()
    gate = threading.Event()
    fake1 = GatedFake(gate=gate, op="fetch")
    mgr1 = _manager(tmp_path, cfg, fake1, clock)
    mgr1.start()
    seat = _ready_seat(mgr1)
    mgr1.release_seat(seat.id, repo="demo", branch="task")
    assert fake1.started.wait(5)
    assert _wait_until(lambda: _row(mgr1, seat.id).state == "releasing")

    fake2 = FakeProvisioner(fail_exports=True)
    fake2.inject_vm(seat.name, seat_type="terminal", state="running")
    mgr2 = _manager(tmp_path, cfg, fake2, clock)
    report = mgr2.reconcile()

    assert report.seats_resumed >= 1
    row = _row(mgr2, seat.id)
    assert row.state == "held"
    assert row.pending_action == "release"
    assert fake2.destroyed == []
    assert seat.id in mgr2.pool_status().needs_attention

    # Operator can still force the pool open.
    forced = mgr2.force_discard(seat.id).result(timeout=5)
    assert forced.state == "off"
    assert fake2.destroyed == [f"fake://{seat.name}"]
    handle = mgr2.request_seat("B", "terminal")
    assert handle.wait_ready(timeout=5).seat.state == "ready"


# --------------------------------------------------------------------------
# FIX 1 - interrupted reset resumes (or falls back to discard)
# --------------------------------------------------------------------------
def test_interrupted_reset_resumes_on_reconcile(tmp_path, clock):
    cfg = _config()
    gate = threading.Event()
    fake1 = GatedFake(gate=gate, op="reset")
    mgr1 = _manager(tmp_path, cfg, fake1, clock)
    mgr1.start()
    seat = _ready_seat(mgr1)

    mgr1.reset_seat(seat.id)
    assert fake1.started.wait(5), "reset never reached the provisioner"
    assert _wait_until(
        lambda: (
            _row(mgr1, seat.id).state == "resetting"
            and _row(mgr1, seat.id).pending_action == "reset"
        )
    )

    fake2 = FakeProvisioner()
    fake2.inject_vm(seat.name, seat_type="terminal", state="running")
    mgr2 = _manager(tmp_path, cfg, fake2, clock)
    report = mgr2.reconcile()

    assert report.seats_resumed >= 1
    row = _row(mgr2, seat.id)
    assert row.state == "ready"
    assert row.pending_action is None
    assert fake2.destroyed == []


def test_interrupted_reset_failure_falls_back_to_discard(tmp_path, clock):
    cfg = _config()
    gate = threading.Event()
    fake1 = GatedFake(gate=gate, op="reset")
    mgr1 = _manager(tmp_path, cfg, fake1, clock)
    mgr1.start()
    seat = _ready_seat(mgr1)
    mgr1.reset_seat(seat.id)
    assert fake1.started.wait(5)
    assert _wait_until(lambda: _row(mgr1, seat.id).state == "resetting")

    fake2 = FakeProvisioner(fail_on={"reset"})
    fake2.inject_vm(seat.name, seat_type="terminal", state="running")
    mgr2 = _manager(tmp_path, cfg, fake2, clock)
    report = mgr2.reconcile()

    assert report.seats_resumed >= 1
    row = _row(mgr2, seat.id)
    assert row.state == "off"  # never wedged in resetting
    assert row.pending_action is None
    assert fake2.destroyed == [f"fake://{seat.name}"]
    handle = mgr2.request_seat("B", "terminal")
    assert handle.wait_ready(timeout=5).seat.state == "ready"


# --------------------------------------------------------------------------
# FIX 1 - operator escape hatches / surfacing
# --------------------------------------------------------------------------
def test_retry_release_resumes_a_held_seat(tmp_path, clock):
    cfg = _config()
    fake = FakeProvisioner(fail_exports=True)
    mgr = _manager(tmp_path, cfg, fake, clock)
    seat = _ready_seat(mgr)

    outcome = mgr.release_seat(seat.id, repo="demo", branch="task").result(timeout=5)
    assert outcome.held is True
    assert _row(mgr, seat.id).state == "held"
    assert seat.id in mgr.pool_status().needs_attention

    fake.fail_exports = False
    retried = mgr.retry_release(seat.id).result(timeout=5)
    assert retried.destroyed is True
    row = _row(mgr, seat.id)
    assert row.state == "off"
    assert row.pending_action is None
    assert mgr.pool_status().needs_attention == []


def test_retry_release_without_intent_is_rejected(tmp_path, clock):
    fake = FakeProvisioner()
    mgr = _manager(tmp_path, _config(), fake, clock)
    seat = _ready_seat(mgr)
    with pytest.raises(ProvisionerError, match="no persisted release intent"):
        mgr.retry_release(seat.id).result(timeout=5)


def test_force_discard_unblocks_a_ready_seat(tmp_path, clock):
    fake = FakeProvisioner()
    mgr = _manager(tmp_path, _config(), fake, clock)
    seat = _ready_seat(mgr)
    view = mgr.force_discard(seat.id, reason="operator").result(timeout=5)
    assert view.state == "off"
    assert fake.destroyed == [f"fake://{seat.name}"]
    handle = mgr.request_seat("B", "terminal")
    assert handle.wait_ready(timeout=5).seat.state == "ready"


def test_unrecoverable_releasing_seat_is_surfaced_not_destroyed(tmp_path, clock):
    """No persisted intent: reconcile surfaces it and force_discard unblocks."""
    cfg = _config()
    fake = FakeProvisioner()
    vm_ref = fake.inject_vm("terminal-1", seat_type="terminal", state="running")
    mgr = _manager(tmp_path, cfg, fake, clock)
    now = st.fmt_time(clock.now)
    with mgr.store.transaction() as conn:
        seat_id = st.insert_seat(
            conn,
            name="terminal-1",
            seat_type="terminal",
            image="golden-term",
            state="releasing",
            vm_name=vm_ref,
            now=now,
        )

    report = mgr.reconcile()
    assert report.seats_resumed == 0
    row = _row(mgr, seat_id)
    assert row.state == "releasing"  # preserved: work may be recoverable
    assert seat_id in mgr.pool_status().needs_attention
    assert fake.destroyed == []

    forced = mgr.force_discard(seat_id).result(timeout=5)
    assert forced.state == "off"
    assert fake.destroyed == [vm_ref]
    handle = mgr.request_seat("B", "terminal")
    assert handle.wait_ready(timeout=5).seat.state == "ready"


# --------------------------------------------------------------------------
# FIX B (round 3) - a release interrupted after the VM is gone is not held
# --------------------------------------------------------------------------
def _seed_pending_release(manager: Manager, *, name: str, vm_ref: str, now: str) -> int:
    with manager.store.transaction() as conn:
        seat_id = st.insert_seat(
            conn,
            name=name,
            seat_type="terminal",
            image="golden-term",
            state="releasing",
            vm_name=vm_ref,
            now=now,
        )
        st.update_seat(
            conn,
            seat_id,
            pending_action="release",
            pending_export=1,
            pending_repo="demo",
            pending_branch="task",
            pending_request_status="done",
            now=now,
        )
    return seat_id


def test_fake_fetch_bundle_fails_for_absent_vm() -> None:
    """The fake must express the gone-VM path (otherwise it masks FIX B)."""
    fake = FakeProvisioner()
    vm = fake.create_from_image("terminal-1", "terminal", "img", ResourceCaps(1, 512, 1))
    assert fake.fetch_bundle(vm, ExportSpec(repo="demo")).ok is True
    fake.drop_vm(vm)
    result = fake.fetch_bundle(vm, ExportSpec(repo="demo"))
    assert result.ok is False
    assert "unknown VM" in result.message


def test_interrupted_release_with_gone_vm_finalizes_off(tmp_path, clock):
    cfg = _config()
    fake = FakeProvisioner()  # deliberately no VM registered
    mgr = _manager(tmp_path, cfg, fake, clock)
    seat_id = _seed_pending_release(
        mgr, name="terminal-1", vm_ref="fake://terminal-1", now=st.fmt_time(clock.now)
    )

    report = mgr.reconcile()
    row = _row(mgr, seat_id)
    assert row.state == "off"  # not held
    assert row.pending_action is None
    # No export was attempted against the vanished VM.
    assert "fetch_bundle" not in fake.call_names()
    assert report.seats_off >= 1
    handle = mgr.request_seat("B", "terminal")
    assert handle.wait_ready(timeout=5).seat.state == "ready"


def test_interrupted_release_with_stopped_vm_finalizes_off(tmp_path, clock):
    cfg = _config()
    fake = FakeProvisioner()
    vm_ref = fake.inject_vm("terminal-1", seat_type="terminal", state="stopped")
    mgr = _manager(tmp_path, cfg, fake, clock)
    seat_id = _seed_pending_release(
        mgr, name="terminal-1", vm_ref=vm_ref, now=st.fmt_time(clock.now)
    )

    report = mgr.reconcile()
    row = _row(mgr, seat_id)
    assert row.state == "off"  # not held
    assert row.pending_action is None
    assert "fetch_bundle" not in fake.call_names()
    assert report.seats_resumed >= 1
    # A stopped-but-defined VM is cleaned up, not leaked.
    assert fake.destroyed == [vm_ref]
    handle = mgr.request_seat("B", "terminal")
    assert handle.wait_ready(timeout=5).seat.state == "ready"


class _AttachBroken(FakeProvisioner):
    """attach() fails transiently; presence is unknowable, not "absent"."""

    def attach(self, vm_ref):  # type: ignore[override]
        raise RuntimeError("transient control-plane error")


def test_resume_with_unknown_vm_state_preserves_work(tmp_path, clock):
    cfg = _config()
    fake = _AttachBroken(fail_exports=True)
    fake.inject_vm("terminal-1", seat_type="terminal", state="running")
    mgr = _manager(tmp_path, cfg, fake, clock)
    seat_id = _seed_pending_release(
        mgr, name="terminal-1", vm_ref="fake://terminal-1", now=st.fmt_time(clock.now)
    )
    # The pump path probes presence with attach(); an exception is "unknown",
    # so the export is attempted (and held on failure) rather than discarded.
    resumed = mgr.scheduler.resume_interrupted(vms=None)
    assert resumed >= 1
    assert _row(mgr, seat_id).state == "held"
    assert "fetch_bundle" in fake.call_names()
