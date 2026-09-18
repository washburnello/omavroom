"""Phase 4A second-round gate regressions (round-2 FIX 1-9).

Each regression targets a specific pre-fix defect; see the docstrings for
how the test would fail on the pre-fix code.
"""

from __future__ import annotations

import threading
import time

import pytest

from omavroom import state as st
from omavroom.config import Config, SeatTypeConfig
from omavroom.manager.provisioner import (
    ExportSpec,
    FakeProvisioner,
    ProvisionerError,
    ResourceCaps,
)
from omavroom.manager.scheduler import Scheduler

CAPS = ResourceCaps(cpu_vcpus=2, memory_mb=2048, overlay_max_gb=10)


def _base_config() -> Config:
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    return cfg


def _scheduler(tmp_path, cfg, clock, *, free_ram_mb, provisioner=None) -> Scheduler:
    store = st.StateStore(tmp_path / "round2.db")
    store.init()
    return Scheduler(
        store,
        cfg,
        provisioner or FakeProvisioner(),
        clock=clock,
        free_ram_mb=free_ram_mb,
    )


def _ready_seat(mgr, label: str = "A"):
    handle = mgr.request_seat(label, "terminal")
    mgr.run_until_idle()
    return mgr.seat_status(handle.request_id).seat


# ---------------------------------------------------------------------------
# FIX 1 - teardown must be exception-safe
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["fetch_bundle", "push"])
def test_fix1_export_raise_holds_and_preserves_vm(make_manager, method):
    """Pre-fix: raise left the seat `releasing` with a live lease and later
    reclaim destroyed unexported work; status was also marked `done`."""
    fake = FakeProvisioner(fail_on={method})
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.held is True and outcome.destroyed is False
    assert fake.destroyed == []
    assert seat.vm_name in fake.vms  # VM preserved for recovery
    view = mgr.seat_status(handle.request_id)
    assert view.seat.state == "held"
    assert view.status == "failed"  # not "done"


def test_fix1_gate_approver_raise_holds(make_manager):
    cfg = _base_config()
    cfg.export.approval_required = True
    fake = FakeProvisioner()

    def broken_approver(fetched):
        raise RuntimeError("approver exploded")

    mgr = make_manager(
        cfg,
        provisioner=fake,
        free_ram_mb=lambda: 10**9,
        export_approver=broken_approver,
    )
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.held is True
    assert "gate error" in outcome.message
    assert fake.destroyed == []
    assert mgr.seat_status(handle.request_id).seat.state == "held"


def test_fix1_stop_raise_still_tears_down(make_manager):
    """A raise in stop must be swallowed; destroy still runs and the seat
    lands off (non-occupying), not stuck `releasing`."""
    fake = FakeProvisioner(raise_on={"stop": OSError("stop failed")})
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    outcome = mgr.release_seat(seat.id, export=False).result(timeout=5)
    assert outcome.destroyed is True
    assert mgr.seat_status(handle.request_id).seat.state == "off"
    assert seat.vm_name in fake.destroyed


def test_fix1_destroy_raise_leaves_terminal_non_occupying(make_manager):
    """A raise in destroy is swallowed; the seat is still off, never stuck
    `releasing` (the VM is preserved and would be reaped by reconcile)."""
    fake = FakeProvisioner(raise_on={"destroy": OSError("destroy failed")})
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    outcome = mgr.release_seat(seat.id, export=False).result(timeout=5)
    assert outcome.destroyed is False  # honest: the VM was not actually removed
    assert mgr.seat_status(handle.request_id).seat.state == "off"
    assert fake.destroyed == []
    assert seat.vm_name in fake.vms


def test_fix1_export_seat_failure_keeps_seat_ready(make_manager):
    fake = FakeProvisioner(fail_on={"fetch_bundle"})
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    outcome = mgr.export_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.ok is False
    view = mgr.seat_status(handle.request_id)
    assert view.seat.state == "ready"
    assert fake.destroyed == []


# ---------------------------------------------------------------------------
# FIX 2 - reconcile must not destroy an in-flight VM / race the pump
# ---------------------------------------------------------------------------
def test_fix2_reconcile_adopts_inflight_vm_by_identity(tmp_path, clock, free_ram):
    """Pre-fix: a VM created during wait_ready (not yet persisted on the
    seat) was destroyed as a false orphan."""
    fake = FakeProvisioner()
    sched = _scheduler(tmp_path, _base_config(), clock, free_ram_mb=free_ram, provisioner=fake)
    with sched.store.transaction() as conn:
        st.insert_seat(
            conn,
            name="terminal-1",
            seat_type="terminal",
            image="img",
            state="provisioning",
            now=st.fmt_time(clock.now),
        )
    fake.inject_vm("terminal-1")  # running, not yet referenced by vm_name

    report = sched.reconcile()
    assert report.orphans_destroyed == 0
    assert "fake://terminal-1" not in fake.destroyed
    assert report.seats_recovered == 1
    seat = sched.store.read(lambda c: st.list_seats(c))[0]
    assert seat.state == "ready" and seat.vm_name == "fake://terminal-1"


def test_fix2_manager_reconcile_serializes_with_pump(make_manager):
    fake = FakeProvisioner(delay_s=0.2)
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    mgr.start()
    try:
        handle = mgr.request_seat("A", "terminal")
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(seat.state == "provisioning" for seat in mgr.list_seats(include_history=True)):
                break
            time.sleep(0.01)
        report = mgr.reconcile()
        assert report.orphans_destroyed == 0
        view = handle.wait_ready(timeout=5)
        assert view.seat.state == "ready"
        assert view.seat.vm_name not in fake.destroyed
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# FIX 3 - reset/sweep lease + resurrection safety
# ---------------------------------------------------------------------------
def test_fix3_reset_conflict_is_not_resurrected(tmp_path, clock, free_ram):
    """Pre-fix: a release landing mid-reset was overwritten to `ready`."""
    fake = FakeProvisioner()
    sched = _scheduler(tmp_path, _base_config(), clock, free_ram_mb=free_ram, provisioner=fake)
    request_id = sched.submit_request("A", "terminal")
    sched.pump()
    seat = sched.request_view(request_id).seat
    original_reset = fake.reset

    def reset_then_release(vm_ref: str) -> None:
        original_reset(vm_ref)
        with sched.store.transaction() as conn:
            now = st.fmt_time(clock.now)
            st.update_seat(conn, seat.id, state="off", vm_name=None, now=now)
            lease = st.active_lease_for_seat(conn, seat.id)
            if lease is not None:
                st.release_lease(conn, lease.id, now)

    fake.reset = reset_then_release
    view = sched.reset_seat(seat.id)
    assert view.state == "off"
    final = sched.store.read(lambda c: st.seat_by_id(c, seat.id))
    assert final.state == "off"


def test_fix3_reset_failure_closes_lease(make_manager):
    fake = FakeProvisioner(fail_on={"reset"})
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    with pytest.raises(ProvisionerError):
        mgr.reset_seat(seat.id).result(timeout=5)
    view = mgr.seat_status(handle.request_id)
    assert view.seat.state == "error"
    assert view.lease is None


def test_fix3_reaper_closes_orphan_lease(tmp_path, clock, free_ram):
    fake = FakeProvisioner()
    sched = _scheduler(tmp_path, _base_config(), clock, free_ram_mb=free_ram, provisioner=fake)
    now = st.fmt_time(clock.now)
    with sched.store.transaction() as conn:
        seat_id = st.insert_seat(
            conn,
            name="terminal-1",
            seat_type="terminal",
            image="img",
            state="error",
            now=now,
        )
        request_id = st.enqueue_request(
            conn, agent_label="A", seat_type="terminal", image=None, project=None, now=now
        )
        st.update_request(conn, request_id, status="claimed", seat_id=seat_id, now=now)
        st.create_lease(
            conn,
            seat_id=seat_id,
            request_id=request_id,
            agent_label="A",
            now=now,
            lease_timeout_s=1800,
        )

    sched.pump()
    assert sched.store.read(lambda c: st.active_lease_for_seat(c, seat_id)) is None
    final = sched.store.read(lambda c: st.seat_by_id(c, seat_id))
    assert final.state == "off"


def test_fix3_release_off_path_closes_lease(tmp_path, clock, free_ram):
    fake = FakeProvisioner()
    sched = _scheduler(tmp_path, _base_config(), clock, free_ram_mb=free_ram, provisioner=fake)
    now = st.fmt_time(clock.now)
    with sched.store.transaction() as conn:
        seat_id = st.insert_seat(
            conn, name="terminal-1", seat_type="terminal", image="img", state="off", now=now
        )
        st.create_lease(
            conn,
            seat_id=seat_id,
            request_id=None,
            agent_label="A",
            now=now,
            lease_timeout_s=1800,
        )

    outcome = sched.release_seat(seat_id, export=False)
    assert outcome.message == "already off"
    assert sched.store.read(lambda c: st.active_lease_for_seat(c, seat_id)) is None


# ---------------------------------------------------------------------------
# FIX 4 - push must receive an explicit destination
# ---------------------------------------------------------------------------
def test_fix4_push_receives_explicit_destination(make_manager, fake):
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.destroyed is True
    push_calls = [args for name, args, _ in fake.calls if name == "push"]
    assert push_calls and push_calls[0][0].repo == "demo"
    assert outcome.export is not None
    assert outcome.export.fetched is not None
    assert outcome.export.fetched.spec.repo == "demo"


# ---------------------------------------------------------------------------
# FIX 5 - eviction must never destroy a seat claimed in the window
# ---------------------------------------------------------------------------
def test_fix5_release_guards_refuse_leased_seat(tmp_path, clock, free_ram):
    """Pre-fix: no lease guard existed at all (TypeError), so eviction could
    destroy a seat claimed after the pre-check."""
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=2)
    fake = FakeProvisioner()
    sched = _scheduler(tmp_path, cfg, clock, free_ram_mb=free_ram, provisioner=fake)
    request_id = sched.submit_request("A", "terminal")
    sched.pump()
    seat = sched.request_view(request_id).seat  # ready + leased

    outcome = sched._release_seat_locked(
        seat.id,
        now=clock.now,
        export=False,
        repo=None,
        request_status=None,
        require_no_lease=True,
        require_state="ready",
    )
    assert outcome.destroyed is False
    assert outcome.message == "seat_now_leased"
    assert fake.destroyed == []


def test_fix5_eviction_leaves_leased_warm_seat_alone(make_manager, fake):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=1, max_seats=2)
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    mgr.run_until_idle()  # prewarm one idle default seat
    first = mgr.request_seat("A", "terminal")  # claims the warm seat
    mgr.run_until_idle()
    leased_seat = mgr.seat_status(first.request_id).seat
    assert leased_seat.lease_expires_at is not None

    pinned = mgr.request_seat("B", "terminal", image="custom")
    mgr.run_until_idle()
    assert mgr.seat_status(pinned.request_id).seat.image == "custom"
    assert leased_seat.vm_name not in fake.destroyed


# ---------------------------------------------------------------------------
# FIX 6 - reconcile must notice a dead VM behind a ready/busy seat
# ---------------------------------------------------------------------------
def test_fix6_stopped_vm_errors_and_reaps(make_manager, fake):
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat
    fake.set_vm_state(seat.vm_name, "stopped")

    report = mgr.reconcile()
    assert report.seats_errored == 1
    view = mgr.seat_status(handle.request_id)
    assert view.seat.state == "error"
    assert "not running" in (view.seat.last_error or "")

    mgr.run_until_idle()
    assert mgr.seat_status(handle.request_id).seat.state == "off"
    assert seat.vm_name in fake.destroyed


# ---------------------------------------------------------------------------
# FIX 7 - a corrupt timestamp must not abort the pump
# ---------------------------------------------------------------------------
def test_fix7_corrupt_timestamp_does_not_abort_pump(make_manager):
    """Pre-fix: st.parse_time raised every pump, aborting the whole pass so
    queued requests were never served."""
    fake = FakeProvisioner()
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    with mgr.store.transaction() as conn:
        st.insert_seat(
            conn,
            name="terminal-99",
            seat_type="terminal",
            image="img",
            state="error",
            now="not-a-date",
        )

    handle = mgr.request_seat("A", "terminal")
    view = handle.wait_ready(timeout=3)

    assert view.seat.state == "ready"
    bad = [s for s in mgr.list_seats(include_history=True) if s.name == "terminal-99"]
    assert bad and bad[0].state == "off"
    assert "quarantined" in (bad[0].last_error or "")


# ---------------------------------------------------------------------------
# FIX 8 - fake fetch/push must honour raise_on
# ---------------------------------------------------------------------------
def test_fix8_fetch_and_push_honor_raise_on():
    fetch_fake = FakeProvisioner(raise_on={"fetch_bundle": OSError("fetch boom")})
    vm = fetch_fake.create_from_image("terminal-1", "terminal", "img", CAPS)
    with pytest.raises(OSError):
        fetch_fake.fetch_bundle(vm, ExportSpec(repo="demo"))

    push_fake = FakeProvisioner(raise_on={"push": RuntimeError("push boom")})
    vm2 = push_fake.create_from_image("terminal-1", "terminal", "img", CAPS)
    fetched = push_fake.fetch_bundle(vm2, ExportSpec(repo="demo"))
    with pytest.raises(RuntimeError):
        push_fake.push(ExportSpec(repo="demo"), fetched)


# ---------------------------------------------------------------------------
# FIX 9 - desktop ops take the seat lock; autostart invariant documented
# ---------------------------------------------------------------------------
def test_fix9_desktop_ops_take_seat_lock(make_manager):
    """Pre-fix: screenshot ignored the seat lock and could race a release."""
    fake = FakeProvisioner()
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    started = threading.Event()
    finished = threading.Event()

    def call_screenshot() -> None:
        started.set()
        mgr.screenshot(seat.id, max_width=160)
        finished.set()

    with mgr.locks.seat(seat.name):
        thread = threading.Thread(target=call_screenshot)
        thread.start()
        assert started.wait(1)
        time.sleep(0.1)
        assert finished.is_set() is False  # blocked on the seat lock
    thread.join(timeout=5)
    assert finished.is_set()


def test_fix9_autostart_never_invariant_documented():
    from omavroom.manager.provisioner import Provisioner

    doc = Provisioner.__doc__ or ""
    assert "Autostart" in doc and "never" in doc.lower()
