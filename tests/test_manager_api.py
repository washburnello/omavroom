"""Phase 4 manager facade: the frozen Phase 5-facing API surface."""

from __future__ import annotations

from omavroom.config import Config, SeatTypeConfig
from omavroom.manager.provisioner import FakeProvisioner


def test_public_api_surface_is_present(manager) -> None:
    expected = [
        "start",
        "stop",
        "tick",
        "list_seats",
        "pool_status",
        "queue_view",
        "seat_status",
        "list_events",
        "request_seat",
        "wait_for_request",
        "cancel_request",
        "set_admission_override",
        "heartbeat",
        "begin_work",
        "finish_work",
        "exec_start",
        "exec_poll",
        "exec_finish",
        "exec_kill",
        "list_execs",
        "screenshot",
        "input",
        "peek_endpoint",
        "reconcile",
        "clear_prewarm_backoff",
        "release_seat",
        "reset_seat",
        "prepare_repo",
        "export_seat",
    ]
    for name in expected:
        assert callable(getattr(manager, name)), name


def test_request_returns_immediately_before_pump(manager) -> None:
    handle = manager.request_seat("agent-1", "terminal")
    assert handle.status().status == "waiting"
    assert manager.list_seats() == []


def test_full_lifecycle_through_the_facade(manager, fake) -> None:
    handle = manager.request_seat("agent-1", "terminal", project="demo")
    view = handle.wait_ready(timeout=5)
    assert view.status == "claimed"
    assert view.seat.state == "ready"
    assert view.project == "demo"
    assert view.queue_ahead == 0
    seat_id = view.seat.id

    started = manager.exec_start(seat_id, "exec-1", label="build")
    assert started.state == "running"
    assert manager.seat_status(handle.request_id).seat.state == "busy"
    assert manager.exec_poll(seat_id, "exec-1").state == "running"

    finished = manager.exec_finish(seat_id, "exec-1", exit_code=0)
    assert finished.state == "finished"
    assert manager.seat_status(handle.request_id).seat.state == "ready"
    assert manager.list_execs(seat_id)[0].exit_code == 0

    lease = manager.heartbeat(seat_id=seat_id)
    assert lease.seat_id == seat_id

    outcome = manager.release_seat(seat_id, export=False).result(timeout=5)
    assert outcome.destroyed is True
    assert fake.destroyed == [view.seat.vm_name]
    assert manager.list_seats() == []
    assert manager.list_seats(include_history=True)[0].state == "off"


def test_background_worker_provisions_without_manual_pumping(make_manager, config, fake):
    config.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=1)
    mgr = make_manager(config, provisioner=fake, free_ram_mb=lambda: 10**9)
    mgr.start()
    try:
        handle = mgr.request_seat("agent-1", "terminal")
        view = handle.wait_ready(timeout=5)
        assert view.seat.state == "ready"
    finally:
        mgr.stop()


def test_queue_view_reports_positions_and_ahead(make_manager):
    cfg = Config.default()
    cfg.seats["terminal"] = SeatTypeConfig(cost_units=1, min_seats=0, max_seats=1)
    mgr = make_manager(cfg, provisioner=FakeProvisioner(), free_ram_mb=lambda: 10**9)
    mgr.request_seat("A", "terminal")
    b = mgr.request_seat("B", "terminal")
    mgr.run_until_idle()

    queue = mgr.queue_view()
    assert [r.agent_label for r in queue] == ["A", "B"]
    assert queue[0].status == "claimed"
    assert queue[1].status == "waiting"
    assert queue[1].queue_ahead == 0

    cancelled = mgr.cancel_request(b.request_id)
    assert cancelled.done() is False  # non-blocking: returns a handle
    cancelled_view = cancelled.result(timeout=5)
    assert cancelled_view.status == "cancelled"
    mgr.run_until_idle()
    # Active view hides finals; history shows them.
    assert [r.status for r in mgr.queue_view()] == ["claimed"]
    assert [r.status for r in mgr.queue_view(include_history=True)] == [
        "claimed",
        "cancelled",
    ]


def test_pool_status_shape(manager) -> None:
    status = manager.pool_status()
    assert status.headroom_floor_mb == manager.config.host.headroom_floor_mb
    assert set(status.per_type) == {"desktop", "terminal"}
    assert status.per_type["terminal"].max_seats == manager.config.seats["terminal"].max_seats
    assert status.admission_override == "auto"


def test_events_are_available_through_facade(manager) -> None:
    handle = manager.request_seat("agent-1", "terminal")
    manager.run_until_idle()
    events = manager.list_events(limit=50)
    types = {event.event_type for event in events}
    assert "request_submitted" in types
    assert "seat_ready" in types
    assert any(event.seat_id == manager.seat_status(handle.request_id).seat.id for event in events)


def test_reset_seat_via_facade(manager, fake) -> None:
    handle = manager.request_seat("agent-1", "terminal")
    manager.run_until_idle()
    seat = manager.seat_status(handle.request_id).seat
    manager.exec_start(seat.id, "exec-1")
    view = manager.reset_seat(seat.id).result(timeout=5)
    assert view.state == "ready"
    assert fake.resets == [seat.vm_name]
    assert fake.destroyed == []
