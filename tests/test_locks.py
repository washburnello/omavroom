"""Phase 4 lock ordering: per-seat / per-repo serialization."""

from __future__ import annotations

import threading

from omavroom.config import Config, SeatTypeConfig
from omavroom.manager.locks import LockManager
from omavroom.manager.provisioner import FakeProvisioner


def _config() -> Config:
    cfg = Config.default()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=2)
    return cfg


def _ready_seat(mgr, label: str):
    handle = mgr.request_seat(label, "terminal")
    mgr.run_until_idle()
    return mgr.seat_status(handle.request_id).seat


def _run_export_threads(calls, errors: list[BaseException]) -> None:
    barrier = threading.Barrier(len(calls))

    def go(fn):
        barrier.wait()
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(fn,)) for fn in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)


def test_documented_global_order_is_seat_then_repo() -> None:
    assert LockManager.GLOBAL_ORDER == ("seat", "repo")


def test_lock_manager_holds_and_releases_in_order() -> None:
    locks = LockManager()
    with locks.seat_then_repo("seat-1", "repo-1"):
        assert locks.seat_held("seat-1")
        assert locks.repo_held("repo-1")
    assert not locks.seat_held("seat-1")
    assert not locks.repo_held("repo-1")


def test_lock_manager_skips_repo_when_absent() -> None:
    locks = LockManager()
    with locks.seat_then_repo("seat-1", None):
        assert locks.seat_held("seat-1")
        assert not locks.repo_held("anything")


def test_concurrent_exports_on_one_seat_serialize(make_manager):
    fake = FakeProvisioner(export_delay_s=0.2)
    mgr = make_manager(_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    seat = _ready_seat(mgr, "A")

    errors: list[BaseException] = []
    results: list = []

    def call_export() -> None:
        results.append(mgr.scheduler.export_seat(seat.id, repo="repo-a"))

    _run_export_threads([call_export, call_export], errors)
    assert errors == []
    assert len(results) == 2
    assert fake.max_active_exports == 1


def test_same_repo_different_seats_serialize(make_manager):
    fake = FakeProvisioner(export_delay_s=0.2)
    mgr = make_manager(_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    seat_a = _ready_seat(mgr, "A")
    seat_b = _ready_seat(mgr, "B")

    errors: list[BaseException] = []

    def export_a() -> None:
        mgr.scheduler.export_seat(seat_a.id, repo="shared-repo")

    def export_b() -> None:
        mgr.scheduler.export_seat(seat_b.id, repo="shared-repo")

    _run_export_threads([export_a, export_b], errors)
    assert errors == []
    assert fake.max_active_exports == 1


def test_different_repos_run_in_parallel(make_manager):
    fake = FakeProvisioner(export_delay_s=0.3)
    mgr = make_manager(_config(), provisioner=fake, free_ram_mb=lambda: 10**9)
    seat_a = _ready_seat(mgr, "A")
    seat_b = _ready_seat(mgr, "B")

    errors: list[BaseException] = []

    def export_a() -> None:
        mgr.scheduler.export_seat(seat_a.id, repo="repo-a")

    def export_b() -> None:
        mgr.scheduler.export_seat(seat_b.id, repo="repo-b")

    _run_export_threads([export_a, export_b], errors)
    assert errors == []
    assert fake.max_active_exports == 2
