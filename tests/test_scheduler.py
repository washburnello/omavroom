"""Phase 4 scheduler behavior against the FakeProvisioner (VM-free)."""

from __future__ import annotations

import threading

import pytest

from omavroom import state as st
from omavroom.config import Config, SeatTypeConfig
from omavroom.manager.provisioner import FakeProvisioner
from omavroom.manager.scheduler import Scheduler


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


# ---------------------------------------------------------------------------
# static bounds + fairness
# ---------------------------------------------------------------------------
def test_over_claim_is_refused(make_manager):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=1)
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    a = mgr.request_seat("A", "terminal")
    b = mgr.request_seat("B", "terminal")
    mgr.run_until_idle()

    va = mgr.seat_status(a.request_id)
    vb = mgr.seat_status(b.request_id)
    assert (va.status, va.seat.state) == ("claimed", "ready")
    assert vb.status == "waiting"
    assert mgr.scheduler.admission("terminal").reason == "max_seats"
    assert len(fake.created) == 1


def test_queue_is_fifo_with_stable_positions(make_manager):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=1)
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    a = mgr.request_seat("A", "terminal")
    b = mgr.request_seat("B", "terminal")
    c = mgr.request_seat("C", "terminal")
    assert [mgr.seat_status(r.request_id).position for r in (a, b, c)] == [1, 2, 3]

    mgr.run_until_idle()
    assert mgr.seat_status(a.request_id).seat.state == "ready"
    assert mgr.seat_status(b.request_id).position == 2
    assert mgr.seat_status(c.request_id).queue_ahead == 1

    seat_a = mgr.seat_status(a.request_id).seat
    mgr.release_seat(seat_a.id, export=False).result(timeout=5)
    mgr.run_until_idle()

    vb = mgr.seat_status(b.request_id)
    vc = mgr.seat_status(c.request_id)
    assert (vb.status, vb.seat.state) == ("claimed", "ready")
    assert vb.position == 2
    assert vc.status == "waiting" and vc.queue_ahead == 0
    assert vb.seat.id != seat_a.id


def test_per_type_min_max_enforced(make_manager):
    cfg = _base_config()
    cfg.seats["desktop"] = SeatTypeConfig(cost_units=4, min_seats=0, max_seats=1)
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=2)
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    for i in range(2):
        mgr.request_seat(f"d{i}", "desktop")
    for i in range(3):
        mgr.request_seat(f"t{i}", "terminal")
    mgr.run_until_idle()

    per_type = mgr.pool_status().per_type
    assert per_type["desktop"].occupying == 1
    assert per_type["desktop"].waiting == 1
    assert per_type["terminal"].occupying == 2
    assert per_type["terminal"].waiting == 1
    assert len(fake.created) == 3


def test_min_seats_prewarms_idle_ready_seats(make_manager):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=1, max_seats=2)
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    mgr.run_until_idle()
    seats = mgr.list_seats()
    assert len(seats) == 1
    assert seats[0].state == "ready"
    assert seats[0].lease_expires_at is None
    assert len(fake.created) == 1

    mgr.run_until_idle()
    assert len(fake.created) == 1


def test_warm_seat_is_reused_without_reprovisioning(make_manager):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=1, max_seats=2)
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    mgr.run_until_idle()
    warm_id = mgr.list_seats()[0].id

    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    view = mgr.seat_status(handle.request_id)
    assert view.seat.id == warm_id
    assert view.seat.state == "ready"
    assert view.lease is not None
    assert len(fake.created) == 1


# ---------------------------------------------------------------------------
# leases / reclaim / wall-clock caps
# ---------------------------------------------------------------------------
def test_dead_lease_is_reclaimed_by_heartbeat_timeout(make_manager, clock):
    """Reclaim is now stasis: the VM is preserved, never destroyed."""
    cfg = _base_config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat
    vm = seat.vm_name

    clock.advance(cfg.leases.heartbeat_timeout_s + 1)
    report = mgr.tick()
    assert report.reclaimed == 1
    assert fake.destroyed == []
    assert fake.vms.get(vm) is not None
    view = mgr.seat_status(handle.request_id)
    assert view.status == "expired"
    assert view.seat.state == "held"
    assert view.seat.last_error == "stale: heartbeat_timeout"
    assert seat.id in mgr.pool_status().needs_attention


def test_wall_clock_cap_reclaims_despite_heartbeats(make_manager, clock):
    cfg = _base_config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat_id = mgr.seat_status(handle.request_id).seat.id

    # Keep heartbeating, but cross the absolute lease deadline.
    for _ in range(cfg.leases.lease_timeout_s // 100):
        clock.advance(100)
        mgr.heartbeat(seat_id=seat_id)
    clock.advance(1)
    report = mgr.tick()
    assert report.reclaimed == 1
    assert mgr.seat_status(handle.request_id).status == "expired"


def test_heartbeat_renews_liveness(make_manager, clock):
    cfg = _base_config()
    mgr = make_manager(cfg, provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat_id = mgr.seat_status(handle.request_id).seat.id
    clock.advance(cfg.leases.heartbeat_timeout_s - 1)
    lease = mgr.heartbeat(seat_id=seat_id)
    assert lease.last_heartbeat == st.fmt_time(clock.now)
    assert mgr.tick().reclaimed == 0


# ---------------------------------------------------------------------------
# release / reset / failure paths
# ---------------------------------------------------------------------------
def test_release_destroys_the_vm(make_manager):
    cfg = _base_config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    outcome = mgr.release_seat(seat.id, export=False).result(timeout=5)
    assert outcome.destroyed is True and outcome.held is False
    assert fake.destroyed == [seat.vm_name]
    assert mgr.seat_status(handle.request_id).status == "done"
    assert mgr.seat_status(handle.request_id).seat.state == "off"


def test_release_never_reuses_a_dirty_seat(make_manager):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=2)
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)

    first = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    first_seat = mgr.seat_status(first.request_id).seat
    mgr.release_seat(first_seat.id, export=False).result(timeout=5)

    second = mgr.request_seat("B", "terminal")
    mgr.run_until_idle()
    second_seat = mgr.seat_status(second.request_id).seat
    assert second_seat.id != first_seat.id
    assert second_seat.vm_name != first_seat.vm_name
    assert len(fake.created) == 2


def test_reset_reverts_without_releasing(make_manager):
    cfg = _base_config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat
    mgr.exec_start(seat.id, "exec-1")
    assert mgr.seat_status(handle.request_id).seat.state == "busy"

    view = mgr.reset_seat(seat.id).result(timeout=5)
    assert view.state == "ready"
    assert view.id == seat.id
    assert fake.resets == [seat.vm_name]
    assert fake.destroyed == []
    assert mgr.seat_status(handle.request_id).lease is not None


def test_provisioning_failure_marks_request_failed_and_reaps_seat(make_manager):
    cfg = _base_config()
    fake = FakeProvisioner(fail_seats={"terminal-1"})
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()

    view = mgr.seat_status(handle.request_id)
    assert view.status == "failed"
    assert view.seat.state in ("error", "off")
    assert view.seat.last_error
    # The failed VM is destroyed by the reaper, never leaked.
    assert fake.destroyed == fake.created


def test_export_failure_holds_seat_for_recovery(make_manager):
    cfg = _base_config()
    fake = FakeProvisioner(fail_exports=True)
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.held is True and outcome.destroyed is False
    assert fake.destroyed == []
    assert seat.vm_name in fake.vms
    assert mgr.seat_status(handle.request_id).seat.state == "held"


def test_export_success_then_destroy(make_manager):
    cfg = _base_config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.destroyed is True
    assert outcome.export is not None and outcome.export.ok
    assert fake.exported == [seat.vm_name]
    assert fake.destroyed == [seat.vm_name]


# ---------------------------------------------------------------------------
# admission math
# ---------------------------------------------------------------------------
def test_admission_respects_headroom_and_override(tmp_path, clock, free_ram):
    cfg = _base_config()
    cfg.host.headroom_floor_mb = 512
    cfg.resources["terminal"].memory_mb = 2048
    free_ram.value = 512 + 2048 - 1
    sched = _scheduler(tmp_path, cfg, clock, free_ram_mb=free_ram)
    assert sched.admission("terminal").reason == "insufficient_ram"

    free_ram.value = 512 + 2048
    assert sched.admission("terminal").admitted is True

    free_ram.value = 0
    sched.set_admission_override("allow")
    assert sched.admission("terminal").admitted is True

    cfg.admission.dynamic = False
    sched.set_admission_override("auto")
    assert sched.admission("terminal").admitted is True

    sched.set_admission_override("deny")
    assert sched.admission("terminal").reason == "override_deny"


def test_static_max_bound_beats_manual_allow(make_manager):
    cfg = _base_config()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=1)
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 0)
    mgr.set_admission_override("allow")
    mgr.request_seat("A", "terminal")
    mgr.request_seat("B", "terminal")
    mgr.run_until_idle()
    assert len(fake.created) == 1
    assert mgr.list_seats()[0].state == "ready"
    assert sum(1 for r in mgr.queue_view() if r.status == "waiting") == 1


def test_unknown_seat_type_rejected(make_manager):
    mgr = make_manager(_base_config(), provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    with pytest.raises(ValueError, match="unknown seat type"):
        mgr.request_seat("A", "toaster")


# ---------------------------------------------------------------------------
# restart + concurrency
# ---------------------------------------------------------------------------
def test_restart_reattaches_to_same_state(make_manager, config):
    fake = FakeProvisioner()
    first = make_manager(config, provisioner=fake, db_name="shared.db", free_ram_mb=lambda: 10**9)
    handle = first.request_seat("A", "terminal")
    first.run_until_idle()
    seat_id = first.seat_status(handle.request_id).seat.id

    second = make_manager(config, provisioner=fake, db_name="shared.db", free_ram_mb=lambda: 10**9)
    view = second.seat_status(handle.request_id)
    assert view.seat.id == seat_id
    assert view.seat.state == "ready"

    outcome = second.release_seat(seat_id, export=False).result(timeout=5)
    assert outcome.destroyed is True
    assert fake.destroyed == [view.seat.vm_name]


def test_atomic_claim_under_concurrency(tmp_path, config, clock):
    config.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=1)
    db = tmp_path / "race.db"
    schedulers = []
    for _ in range(2):
        store = st.StateStore(db)
        store.init()
        schedulers.append(
            Scheduler(
                store,
                config,
                FakeProvisioner(),
                clock=clock,
                free_ram_mb=lambda: 10**9,
            )
        )
    errors: list[BaseException] = []

    def worker(sched: Scheduler, label: str) -> None:
        try:
            sched.submit_request(label, "terminal")
            for _ in range(6):
                sched.pump()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(schedulers[0], "A")),
        threading.Thread(target=worker, args=(schedulers[1], "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    checking = st.StateStore(db)
    occupying = checking.read(lambda c: st.count_occupying(c, "terminal"))
    waiting = checking.read(lambda c: st.count_waiting(c, "terminal"))
    assert occupying == 1
    assert waiting == 1
