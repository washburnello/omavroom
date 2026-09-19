"""Seat stasis: a stale lease holds (preserves) the VM instead of destroying it.

Part of the seat-lifetime automation: reclaim no longer destroys a seat whose
heartbeat lapsed. The VM and overlay survive in the existing ``held`` state,
the reason is recorded, and the normal gated export is attempted when a durable
export intent exists.
"""

from __future__ import annotations

import time

import pytest

from omavroom import state as st
from omavroom.cli import main as cli_main
from omavroom.config import Config, LeaseConfig
from omavroom.manager.provisioner import FakeProvisioner


def _config() -> Config:
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    return cfg


def _ready(mgr, label: str = "A"):
    handle = mgr.request_seat(label, "terminal")
    mgr.run_until_idle()
    return mgr.seat_status(handle.request_id).seat


def _seat(mgr, seat_id: int):
    return next(s for s in mgr.list_seats(include_history=True) if s.id == seat_id)


def _raw_seat(mgr, seat_id: int):
    return mgr.store.read(lambda c: st.seat_by_id(c, seat_id))


def _lapse(mgr, clock, cfg) -> None:
    clock.advance(cfg.leases.heartbeat_timeout_s + 1)
    mgr.tick()


def test_stasis_preserves_vm_and_surfaces_reason(make_manager, clock):
    cfg = _config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)

    _lapse(mgr, clock, cfg)

    assert fake.destroyed == []
    assert seat.vm_name in fake.vms
    view = _seat(mgr, seat.id)
    assert view.state == "held"
    assert view.last_error == "stale: heartbeat_timeout"
    assert view.vm_name == seat.vm_name
    assert seat.id in mgr.pool_status().needs_attention
    assert "seat_stasis" in {e.event_type for e in mgr.list_events(limit=50)}


def test_stasis_without_intent_does_not_export(make_manager, clock):
    cfg = _config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)

    _lapse(mgr, clock, cfg)

    assert fake.exported == []
    assert "fetch_bundle" not in fake.call_names()
    assert fake.destroyed == []
    assert _seat(mgr, seat.id).state == "held"


def test_stasis_auto_exports_recorded_intent(make_manager, clock):
    cfg = _config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)
    mgr.scheduler.record_export_intent(seat.id, repo="demo", branch="task", ref="origin:task")

    _lapse(mgr, clock, cfg)

    assert fake.exported == [seat.vm_name]
    assert "push" in fake.call_names()
    assert fake.destroyed == []  # a successful export never auto-destroys
    assert _seat(mgr, seat.id).state == "held"
    assert "seat_stasis_export" in {e.event_type for e in mgr.list_events(limit=50)}


def test_prepare_repo_url_is_not_an_export_intent(make_manager, clock):
    """Regression: a clone URL must never become the durable export repo.

    ``RepoSpec.url`` is a git clone source fed to ``git clone -- <url>`` on the
    guest, whereas the real push treats ``ExportSpec.repo`` as a **host repo
    path** (``git -C <host_repo>``). Recording the URL made stasis auto-export
    impossible against a real provisioner; the fake masked it by treating
    ``repo`` as an opaque key.
    """
    from omavroom.manager.provisioner import RepoSpec

    cfg = _config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)
    mgr.prepare_repo(
        seat.id, RepoSpec(url="https://example.invalid/demo.git", branch="task")
    ).result(timeout=5)

    assert _raw_seat(mgr, seat.id).export_repo is None

    _lapse(mgr, clock, cfg)

    # No intent -> no push at all; the VM is preserved in stasis.
    assert fake.exported == []
    assert "fetch_bundle" not in fake.call_names()
    assert "push" not in fake.call_names()
    assert fake.destroyed == []
    assert _seat(mgr, seat.id).state == "held"


def test_export_seat_records_host_path_used_by_stasis_push(make_manager, clock):
    """The explicit export's host path is what stasis later pushes to."""
    cfg = _config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)
    host_path = "/srv/git/demo"
    mgr.export_seat(seat.id, repo=host_path, branch="task", ref="origin:task").result(timeout=5)
    assert _raw_seat(mgr, seat.id).export_repo == host_path

    _lapse(mgr, clock, cfg)

    # Both the explicit export and the stasis auto-export push to the host path.
    pushes = [args[0] for name, args, _ in fake.calls if name == "push"]
    assert pushes and all(spec.repo == host_path for spec in pushes)
    assert len(pushes) >= 2
    assert _seat(mgr, seat.id).state == "held"


def test_stasis_failed_auto_export_records_error(make_manager, clock):
    cfg = _config()
    fake = FakeProvisioner(fail_exports=True)
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)
    mgr.scheduler.record_export_intent(seat.id, repo="/srv/git/demo")

    _lapse(mgr, clock, cfg)

    assert fake.destroyed == []
    assert seat.vm_name in fake.vms
    view = _seat(mgr, seat.id)
    assert view.state == "held"
    # G7: the failure reason is visible to the operator, stale context kept.
    assert "stale: heartbeat_timeout" in view.last_error
    assert "export failed" in view.last_error
    assert seat.id in mgr.pool_status().needs_attention


def test_stasis_retry_release_exports_then_destroys(make_manager, clock):
    cfg = _config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)
    mgr.scheduler.record_export_intent(seat.id, repo="demo", branch="task")
    _lapse(mgr, clock, cfg)
    assert _seat(mgr, seat.id).state == "held"

    outcome = mgr.retry_release(seat.id).result(timeout=5)

    assert outcome.destroyed is True
    assert fake.exported[-1] == seat.vm_name
    assert fake.destroyed == [seat.vm_name]
    assert mgr.list_seats() == []
    assert mgr.pool_status().needs_attention == []


def test_stasis_retry_release_without_intent_destroys(make_manager, clock):
    cfg = _config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)
    _lapse(mgr, clock, cfg)

    outcome = mgr.retry_release(seat.id).result(timeout=5)

    assert outcome.destroyed is True
    assert fake.exported == []
    assert fake.destroyed == [seat.vm_name]


def test_stasis_force_discard_destroys(make_manager, clock):
    cfg = _config()
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)
    _lapse(mgr, clock, cfg)

    view = mgr.force_discard(seat.id, reason="operator").result(timeout=5)

    assert view.state == "off"
    assert fake.destroyed == [seat.vm_name]
    assert fake.exported == []  # force_discard never exports


def test_held_ttl_zero_keeps_indefinitely(make_manager, clock):
    cfg = _config()
    assert cfg.leases.held_ttl_s == 0
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)
    _lapse(mgr, clock, cfg)

    clock.advance(10**6)
    report = mgr.tick()

    assert report.held_reaped == 0
    assert _seat(mgr, seat.id).state == "held"
    assert fake.destroyed == []


def test_held_ttl_discards_after_ttl(make_manager, clock):
    cfg = _config()
    cfg.leases.held_ttl_s = 100
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr)
    _lapse(mgr, clock, cfg)
    assert _seat(mgr, seat.id).state == "held"

    clock.advance(99)
    assert mgr.tick().held_reaped == 0
    assert _seat(mgr, seat.id).state == "held"

    clock.advance(2)
    assert mgr.tick().held_reaped == 1
    assert _seat(mgr, seat.id).state == "off"
    assert fake.destroyed == [seat.vm_name]


def test_stasis_still_occupies_capacity(make_manager, clock):
    cfg = _config()
    cfg.seats["terminal"].max_seats = 1
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready(mgr, "A")
    _lapse(mgr, clock, cfg)
    assert _seat(mgr, seat.id).state == "held"

    other = mgr.request_seat("B", "terminal")
    mgr.run_until_idle()

    assert mgr.seat_status(other.request_id).status == "waiting"


def test_stasis_no_vm_seat_finalizes_to_off(make_manager, clock):
    """G2: a stale seat with no VM is finalized, never parked in ``held``.

    A ``queued``/``provisioning`` seat has no VM to preserve, so holding it
    would occupy capacity forever with ``held_ttl_s=0``. It must be closed
    (lease released, reason recorded) and free its slot.
    """
    cfg = _config()
    cfg.seats["terminal"].max_seats = 1
    fake = FakeProvisioner()
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    with mgr.store.transaction() as conn:
        seat_id = st.insert_seat(
            conn,
            name="terminal-stuck",
            seat_type="terminal",
            image="img",
            state=st.SeatState.QUEUED.value,
            now=st.fmt_time(clock.now),
        )
        st.create_lease(
            conn,
            seat_id=seat_id,
            request_id=None,
            agent_label="A",
            now=st.fmt_time(clock.now),
            lease_timeout_s=cfg.leases.lease_timeout_s,
        )

    clock.advance(cfg.leases.heartbeat_timeout_s + 1)
    report = mgr.tick()

    assert report.reclaimed == 1
    view = _seat(mgr, seat_id)
    assert view.state == "off"
    assert view.vm_name is None
    assert view.last_error == "stale: heartbeat_timeout"
    assert seat_id not in mgr.pool_status().needs_attention
    assert mgr.store.read(lambda c: st.active_lease_for_seat(c, seat_id)) is None
    assert fake.destroyed == []

    # The freed slot is reusable: a new request is admitted, not left waiting.
    other = mgr.request_seat("B", "terminal")
    mgr.run_until_idle()
    assert mgr.seat_status(other.request_id).seat.state == "ready"


# ---------------------------------------------------------------------------
# config surface / CLI visibility
# ---------------------------------------------------------------------------
def test_held_ttl_config_validation():
    assert LeaseConfig().held_ttl_s == 0
    with pytest.raises(ValueError, match="held_ttl_s must be >= 0"):
        LeaseConfig(held_ttl_s=-1)
    assert Config.validate_value("leases", "held_ttl_s", 60) == 60
    with pytest.raises(ValueError, match="held_ttl_s must be >= 0"):
        Config.default().set_value("leases", "held_ttl_s", -5)


def test_held_ttl_surfaces_in_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OMAVROOM_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli_main(["settings"]) == 0
    assert "held_ttl_s=0" in capsys.readouterr().out


def test_cli_status_shows_stasis_reason(fake_daemon, capsys):
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    cfg.leases.heartbeat_interval_s = 1
    cfg.leases.heartbeat_timeout_s = 1
    cfg.leases.lease_timeout_s = 3600
    with fake_daemon(config=cfg) as pool:
        handle = pool.manager.request_seat("A", "terminal")
        view = handle.wait_ready(timeout=10)
        assert view.seat is not None and view.seat.state == "ready"
        seat_id = view.seat.id
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            rows = pool.manager.list_seats(include_history=True)
            if any(s.id == seat_id and s.state == "held" for s in rows):
                break
            time.sleep(0.05)
        assert cli_main(["--socket", str(pool.socket_path), "status"]) == 0
        out = capsys.readouterr().out
    assert "held" in out
    assert "stale: heartbeat_timeout" in out
