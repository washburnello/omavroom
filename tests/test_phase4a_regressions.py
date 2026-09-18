"""Phase 4A gate regressions, grouped by audit fix (FIX 1-9)."""

from __future__ import annotations

import threading
import time

import pytest

from omavroom import state as st
from omavroom.config import Config, SeatTypeConfig
from omavroom.manager.provisioner import FakeProvisioner, InputEvent, ProvisionerError
from omavroom.manager.scheduler import PumpReport, Scheduler


def _base_config() -> Config:
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    return cfg


def _scheduler(tmp_path, cfg, clock, *, free_ram_mb, provisioner=None) -> Scheduler:
    store = st.StateStore(tmp_path / "sched.db")
    store.init()
    return Scheduler(
        store,
        cfg,
        provisioner or FakeProvisioner(),
        clock=clock,
        free_ram_mb=free_ram_mb,
    )


def _ready_seat(mgr, label: str = "A", seat_type: str = "terminal"):
    handle = mgr.request_seat(label, seat_type)
    mgr.run_until_idle()
    return mgr.seat_status(handle.request_id).seat


# ---------------------------------------------------------------------------
# FIX 1 - worker/pump must survive failures
# ---------------------------------------------------------------------------
def test_fix1_provisioner_exception_does_not_kill_the_pump(make_manager, fake):
    fake.raise_on = {"start": OSError("libvirt exploded")}
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()

    view = mgr.seat_status(handle.request_id)
    assert view.status == "failed"
    assert fake.destroyed == fake.created  # created VM was cleaned up
    # The pump did not raise; the manager is still healthy.
    assert mgr.last_pump_error is None
    assert mgr.tick().total == 0


def test_fix1_tick_swallows_pump_exception(monkeypatch, make_manager):
    mgr = make_manager(_base_config(), provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)

    def boom():
        raise OSError("pump exploded")

    monkeypatch.setattr(mgr.scheduler, "pump", boom)
    report = mgr.tick()
    assert isinstance(report, PumpReport) and report.total == 0
    assert isinstance(mgr.last_pump_error, OSError)


def test_fix1_worker_survives_a_pump_exception(make_manager, config, clock):
    mgr = make_manager(_base_config(), provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    real_pump = mgr.scheduler.pump
    calls = {"n": 0}

    def flaky_pump():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real_pump()

    mgr.scheduler.pump = flaky_pump
    mgr.start()
    try:
        handle = mgr.request_seat("A", "terminal")
        view = handle.wait_ready(timeout=5)
        assert view.seat.state == "ready"
        assert calls["n"] >= 2
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# FIX 2 - provisioning holds the seat lock; reclaim re-checks liveness
# ---------------------------------------------------------------------------
def test_fix2_release_during_slow_provision_does_not_resurrect(tmp_path, clock, free_ram):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=1)
    fake = FakeProvisioner(delay_s=0.2)
    store = st.StateStore(tmp_path / "race.db")
    store.init()
    sched = Scheduler(store, cfg, fake, clock=clock, free_ram_mb=free_ram)
    sched.submit_request("A", "terminal")

    pump_thread = threading.Thread(target=sched.pump)
    pump_thread.start()

    seat = None
    deadline = time.time() + 5
    while time.time() < deadline:
        provisioning = store.read(
            lambda c: [s for s in st.list_seats(c) if s.state == "provisioning"]
        )
        if provisioning:
            seat = provisioning[0]
            break
        time.sleep(0.01)
    assert seat is not None

    outcome = sched.release_seat(seat.id, export=False)
    pump_thread.join(timeout=5)
    assert pump_thread.is_alive() is False

    final = store.read(lambda c: st.seat_by_id(c, seat.id))
    assert final.state == "off"
    assert outcome.destroyed is True
    assert fake.destroyed.count(f"fake://{seat.name}") == 1


def test_fix2_heartbeat_in_reclaim_window_saves_lease(monkeypatch, make_manager, clock):
    cfg = _base_config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat_id = mgr.seat_status(handle.request_id).seat.id

    clock.advance(cfg.leases.heartbeat_timeout_s + 1)
    real_expired = st.expired_leases

    def expired_then_heartbeat(conn, now, heartbeat_timeout_s):
        rows = real_expired(conn, now, heartbeat_timeout_s)
        # Simulate a heartbeat landing right after the snapshot was computed.
        mgr.heartbeat(seat_id=seat_id)
        return rows

    monkeypatch.setattr(st, "expired_leases", expired_then_heartbeat)
    reclaimed = mgr.scheduler.reclaim_expired(now=clock.now)

    assert reclaimed == 0
    assert fake.destroyed == []
    assert mgr.seat_status(handle.request_id).seat.state == "ready"


def _instrument_serialization(fake: FakeProvisioner) -> dict:
    state = {"active": 0, "max": 0}
    guard = threading.Lock()
    original_reset, original_destroy = fake.reset, fake.destroy

    def wrap(fn):
        def inner(*args, **kwargs):
            with guard:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            try:
                time.sleep(0.05)
                return fn(*args, **kwargs)
            finally:
                with guard:
                    state["active"] -= 1

        return inner

    fake.reset = wrap(original_reset)
    fake.destroy = wrap(original_destroy)
    return state


def test_fix2_reset_and_release_serialize(tmp_path, clock, free_ram):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=1)
    fake = FakeProvisioner(delay_s=0.05)
    sched = _scheduler(tmp_path, cfg, clock, free_ram_mb=free_ram, provisioner=fake)
    request_id = sched.submit_request("A", "terminal")
    sched.pump()
    seat = sched.request_view(request_id).seat
    serial = _instrument_serialization(fake)
    errors: list[BaseException] = []

    def run(fn):
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(lambda: sched.reset_seat(seat.id),)),
        threading.Thread(target=run, args=(lambda: sched.release_seat(seat.id, export=False),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(thread.is_alive() is False for thread in threads)
    assert serial["max"] <= 1
    assert all(isinstance(exc, ProvisionerError) for exc in errors)
    final = sched.store.read(lambda c: st.seat_by_id(c, seat.id))
    assert final.state in ("ready", "off")


# ---------------------------------------------------------------------------
# FIX 3 - bounded prewarm failures + error reaper
# ---------------------------------------------------------------------------
def test_fix3_broken_prewarm_is_bounded_and_reaped(make_manager):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=1, max_seats=1)
    cfg.prewarm.max_retries = 1
    cfg.prewarm.backoff_s = 0
    fake = FakeProvisioner(fail_on={"start"})
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)

    for _ in range(25):
        mgr.tick()

    rows = mgr.list_seats(include_history=True)
    assert len(rows) <= 1  # no unbounded row growth
    assert len(fake.created) <= cfg.prewarm.max_retries + 1
    assert set(fake.created) <= set(fake.destroyed)  # no leaked error VMs


def test_fix3_request_error_seat_is_reaped(make_manager):
    fake = FakeProvisioner(fail_seats={"terminal-1"})
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat
    assert seat.state == "off"
    assert fake.destroyed == fake.created


# ---------------------------------------------------------------------------
# FIX 4 - reattach / adopt on start
# ---------------------------------------------------------------------------
def test_fix4_orphan_vms_are_reaped(make_manager, fake):
    fake.inject_vm("orphan-1", seat_type="terminal")
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    report = mgr.reconcile()
    assert report.orphans_destroyed == 1
    assert "fake://orphan-1" in fake.destroyed


def test_fix4_dangling_seat_ref_goes_to_error(make_manager, fake):
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat
    fake.drop_vm(seat.vm_name)

    report = mgr.reconcile()
    assert report.seats_errored == 1
    view = mgr.seat_status(handle.request_id)
    assert view.seat.state == "error"
    assert view.status == "failed"


def test_fix4_provisioning_seat_with_running_vm_is_recovered(make_manager, fake):
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat
    with mgr.store.transaction() as conn:
        st.update_seat(
            conn,
            seat.id,
            state="provisioning",
            now=st.fmt_time(mgr.scheduler._now()),
        )
    report = mgr.reconcile()
    assert report.seats_recovered == 1
    assert mgr.seat_status(handle.request_id).seat.state == "ready"


def test_fix4_start_reconciles(make_manager, fake):
    fake.inject_vm("orphan-start")
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    mgr.start()
    try:
        assert "fake://orphan-start" in fake.destroyed
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# FIX 5 - content gate between fetch and push
# ---------------------------------------------------------------------------
def test_fix5_destructive_diffstat_is_held(make_manager):
    cfg = _base_config()
    fake = FakeProvisioner()
    fake.export_deletions = cfg.export.max_deletions + 1
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)

    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.held is True and outcome.destroyed is False
    assert "deletions" in outcome.message
    assert "push" not in fake.call_names()
    held = [s for s in mgr.list_seats() if s.id == seat.id]
    assert held and held[0].state == "held"


def test_fix5_protected_path_is_held(make_manager):
    cfg = _base_config()
    fake = FakeProvisioner()
    fake.export_paths = (".github/workflows/ci.yml",)
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)

    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.held is True
    assert "protected path" in outcome.message
    assert "push" not in fake.call_names()


def test_fix5_nonempty_stash_is_held(make_manager):
    fake = FakeProvisioner()
    fake.export_stash_count = 1
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)
    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.held is True
    assert "stash" in outcome.message


def test_fix5_approval_required_holds_without_approver(make_manager):
    cfg = _base_config()
    cfg.export.approval_required = True
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)
    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.held is True
    assert "approval" in outcome.message


def test_fix5_approver_allows_export(make_manager):
    cfg = _base_config()
    cfg.export.approval_required = True
    fake = FakeProvisioner()
    mgr = make_manager(
        cfg,
        provisioner=fake,
        free_ram_mb=lambda: 10**9,
        export_approver=lambda fetched: True,
    )
    seat = _ready_seat(mgr)
    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.destroyed is True
    assert "push" in fake.call_names()


def test_fix5_normal_export_pushes_verifies_and_destroys(make_manager):
    fake = FakeProvisioner()
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)
    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.destroyed is True
    assert outcome.export is not None and outcome.export.ok
    assert fake.call_names().count("push") == 1
    assert fake.destroyed == [seat.vm_name]


def test_fix5_sha_mismatch_holds(make_manager):
    fake = FakeProvisioner(sha_mismatch=True)
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)
    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.held is True
    assert fake.destroyed == []


# ---------------------------------------------------------------------------
# FIX 6 - exec kill + desktop ops through the facade
# ---------------------------------------------------------------------------
def test_fix6_exec_kill_returns_seat_to_ready(make_manager):
    mgr = make_manager(_base_config(), provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat_id = mgr.seat_status(handle.request_id).seat.id

    mgr.exec_start(seat_id, "e1")
    assert mgr.seat_status(handle.request_id).seat.state == "busy"
    killed = mgr.exec_kill(seat_id, "e1")
    assert killed.state == "killed" and killed.exit_code == -9
    assert mgr.seat_status(handle.request_id).seat.state == "ready"


def test_fix6_max_concurrent_execs_enforced(make_manager):
    cfg = _base_config()
    cfg.exec.max_concurrent_per_seat = 1
    mgr = make_manager(cfg, provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat_id = mgr.seat_status(handle.request_id).seat.id
    mgr.exec_start(seat_id, "e1")
    with pytest.raises(ValueError):
        mgr.exec_start(seat_id, "e2")


def test_fix6_desktop_ops_wrappers(make_manager):
    mgr = make_manager(_base_config(), provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "desktop")
    mgr.run_until_idle()
    seat_id = mgr.seat_status(handle.request_id).seat.id

    shot = mgr.screenshot(seat_id, max_width=320)
    assert isinstance(shot, bytes) and b"320x" in shot
    mgr.input(seat_id, [InputEvent("key", "Return")])
    assert mgr.peek_endpoint(seat_id).startswith("vnc://")


# ---------------------------------------------------------------------------
# FIX 7 - cancel is async and atomic
# ---------------------------------------------------------------------------
def test_fix7_cancel_waiting_is_non_blocking_and_atomic(make_manager):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=1)
    mgr = make_manager(cfg, provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    first = mgr.request_seat("A", "terminal")
    second = mgr.request_seat("B", "terminal")
    mgr.run_until_idle()

    handle = mgr.cancel_request(second.request_id)
    assert handle.done() is False
    view = handle.result(timeout=5)
    assert view.status == "cancelled"
    assert [r.status for r in mgr.queue_view()] == ["claimed"]
    assert first.request_id != second.request_id


def test_fix7_cancel_after_ready_releases_and_destroys(make_manager, fake):
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    view = mgr.cancel_request(handle.request_id).result(timeout=5)
    assert view.status == "cancelled"
    assert view.seat.state == "off"
    assert fake.destroyed == [seat.vm_name]


def test_fix7_cancel_racing_slow_provision_loses_nothing(make_manager):
    fake = FakeProvisioner(delay_s=0.15)
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    mgr.start()
    try:
        handle = mgr.request_seat("A", "terminal")
        deadline = time.time() + 5
        while time.time() < deadline:
            seats = mgr.list_seats(include_history=True)
            if any(seat.state == "provisioning" for seat in seats):
                break
            time.sleep(0.01)
        view = mgr.cancel_request(handle.request_id).result(timeout=5)
        assert view.status == "cancelled"
        assert view.seat.state == "off"
        assert fake.destroyed == fake.created
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# FIX 8 - idle seats are evictable for a pinned-image queue head
# ---------------------------------------------------------------------------
def test_fix8_pinned_image_evicts_idle_prewarm(make_manager, fake):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=1, max_seats=1)
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    mgr.run_until_idle()
    warm = mgr.list_seats()[0]
    assert warm.image == mgr.config.image_for("terminal")

    handle = mgr.request_seat("A", "terminal", image="custom-image")
    mgr.run_until_idle()

    view = mgr.seat_status(handle.request_id)
    assert view.seat.state == "ready"
    assert view.seat.image == "custom-image"
    assert f"fake://{warm.name}" in fake.destroyed
    assert any(vm == view.seat.vm_name for vm in fake.created)


# ---------------------------------------------------------------------------
# FIX 9 - bounded read APIs
# ---------------------------------------------------------------------------
def test_fix9_list_seats_hides_history_by_default(make_manager):
    mgr = make_manager(_base_config(), provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat
    mgr.release_seat(seat.id, export=False).result(timeout=5)

    assert mgr.list_seats() == []
    history = mgr.list_seats(include_history=True)
    assert [s.state for s in history] == ["off"]


def test_fix9_queue_view_hides_finals_by_default(make_manager):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=1)
    mgr = make_manager(cfg, provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    mgr.request_seat("A", "terminal")
    b = mgr.request_seat("B", "terminal")
    mgr.run_until_idle()
    mgr.cancel_request(b.request_id).result(timeout=5)

    assert [r.status for r in mgr.queue_view()] == ["claimed"]
    assert [r.status for r in mgr.queue_view(include_history=True)] == [
        "claimed",
        "cancelled",
    ]


def test_fix9_list_events_is_bounded_by_limit(make_manager):
    mgr = make_manager(_base_config(), provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    for index in range(5):
        mgr.request_seat(f"agent-{index}", "terminal")
    mgr.run_until_idle()

    bounded = mgr.list_events(limit=3)
    assert len(bounded) == 3
    everything = mgr.list_events(limit=None)
    assert len(everything) >= len(bounded)
    assert [event.id for event in bounded] == [event.id for event in everything[-3:]]


# ---------------------------------------------------------------------------
# FIX 10 - dead code removed / scope documented
# ---------------------------------------------------------------------------
def test_fix10_dead_helpers_removed() -> None:
    from omavroom import state as st_mod
    from omavroom.manager import locks as locks_mod
    from omavroom.manager.scheduler import Scheduler

    assert not hasattr(st_mod, "seat_by_name")
    assert not hasattr(st_mod, "list_active_leases")
    assert not hasattr(Scheduler, "has_work")
    assert locks_mod.__all__ == ["LockManager"]


def test_fix3_backoff_delays_retry_without_giving_up(make_manager, clock):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=1, max_seats=1)
    cfg.prewarm.max_retries = 1
    cfg.prewarm.backoff_s = 100
    fake = FakeProvisioner(fail_on={"start"})
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)

    mgr.tick()  # creates the prewarm seat; provisioning fails -> error
    first = mgr.list_seats(include_history=True)
    assert [(s.state, s.attempts) for s in first] == [("error", 1)]

    for _ in range(3):
        mgr.tick()  # still inside backoff: must stay error, not suspend
    still = mgr.list_seats(include_history=True)
    assert [(s.state, s.attempts) for s in still] == [("error", 1)]

    clock.advance(cfg.prewarm.backoff_s + 1)
    mgr.tick()  # retry runs and fails again -> attempts=2
    clock.advance(cfg.prewarm.backoff_s + 1)
    mgr.tick()  # attempts exhausted -> suspended + off
    final = mgr.list_seats(include_history=True)
    assert [s.state for s in final] == ["off"]
    assert len(fake.created) == cfg.prewarm.max_retries + 1
    assert set(fake.created) <= set(fake.destroyed)
