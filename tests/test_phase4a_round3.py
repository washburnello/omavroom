"""Phase 4A final gate regressions (G5, FIX A, FIX B).

Each test is written to fail on the pre-fix code; see docstrings.
"""

from __future__ import annotations

from omavroom import state as st
from omavroom.config import Config
from omavroom.manager.provisioner import FakeProvisioner
from omavroom.manager.scheduler import Scheduler


def _base_config() -> Config:
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    return cfg


def _scheduler(tmp_path, cfg, clock, *, free_ram_mb, provisioner=None) -> Scheduler:
    store = st.StateStore(tmp_path / "round3.db")
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
# FIX G5 - reconcile must not leak a defined-but-not-running in-flight VM
# ---------------------------------------------------------------------------
def test_g5_defined_inflight_vm_is_destroyed(tmp_path, clock, free_ram):
    """Pre-fix: the non-running branch wrote ERROR without destroying the VM
    or recording its ref, so the defined domain and its overlay leaked until
    a second reconcile."""
    fake = FakeProvisioner()
    sched = _scheduler(tmp_path, _base_config(), clock, free_ram_mb=free_ram, provisioner=fake)
    now = st.fmt_time(clock.now)
    with sched.store.transaction() as conn:
        seat_id = st.insert_seat(
            conn,
            name="terminal-1",
            seat_type="terminal",
            image="img",
            state="provisioning",
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
    ref = fake.inject_vm("terminal-1", state="defined")

    report = sched.reconcile()

    assert report.seats_errored == 1
    assert fake.attach(ref) is None  # destroyed during reconcile, not leaked
    seat = sched.store.read(lambda c: st.seat_by_id(c, seat_id))
    assert seat.state == "error"
    assert seat.vm_name == ref  # ref persisted for audit


# ---------------------------------------------------------------------------
# FIX A - destroyed / orphans_destroyed must reflect reality
# ---------------------------------------------------------------------------
def test_a_destroy_failure_reports_not_destroyed(make_manager):
    """Pre-fix: a swallowed destroy still reported destroyed=True and
    orphans_destroyed counted attempts."""
    fake = FakeProvisioner(raise_on={"destroy": OSError("destroy failed")})
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)

    outcome = mgr.release_seat(seat.id, export=False).result(timeout=5)
    assert outcome.destroyed is False
    assert fake.attach(seat.vm_name) is not None  # still discoverable

    report = mgr.reconcile()
    assert report.orphans_destroyed == 0
    assert fake.attach(seat.vm_name) is not None


def test_a_successful_destroy_reports_true(make_manager):
    fake = FakeProvisioner()
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)
    outcome = mgr.release_seat(seat.id, export=False).result(timeout=5)
    assert outcome.destroyed is True
    assert fake.attach(seat.vm_name) is None


# ---------------------------------------------------------------------------
# FIX B - successful fetch must carry a non-null SHA
# ---------------------------------------------------------------------------
def test_b_null_sha_holds_without_push(make_manager):
    """Pre-fix: push was skipped verification and reported ok, so the release
    destroyed the VM even though no SHA could be verified."""
    fake = FakeProvisioner()
    fake.sha_prefix = None
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr)

    outcome = mgr.release_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.held is True and outcome.destroyed is False
    assert "SHA" in outcome.message
    assert "push" not in fake.call_names()
    assert fake.attach(seat.vm_name) is not None  # preserved for recovery


def test_b_null_sha_export_seat_keeps_ready(make_manager):
    fake = FakeProvisioner()
    fake.sha_prefix = None
    mgr = make_manager(_base_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    handle = mgr.request_seat("A", "terminal")
    mgr.run_until_idle()
    seat = mgr.seat_status(handle.request_id).seat

    outcome = mgr.export_seat(seat.id, repo="demo").result(timeout=5)
    assert outcome.ok is False
    assert mgr.seat_status(handle.request_id).seat.state == "ready"
    assert fake.destroyed == []
