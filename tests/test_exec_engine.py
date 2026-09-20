"""Phase 5 exec engine: real execution, streaming, kill, timeout, non-blocking.

Everything runs against an in-process Manager + FakeProvisioner; no VM is
booted and no host resource is touched.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from omavroom import state as st
from omavroom.config import Config
from omavroom.manager.execs import ExecNotAllowed
from omavroom.manager.libvirt_provisioner import _subprocess_stream_runner
from omavroom.manager.provisioner import FakeProvisioner


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _ready_seat(manager, label: str = "agent-1", seat_type: str = "terminal"):
    handle = manager.request_seat(label, seat_type)
    view = handle.wait_ready(timeout=5)
    return view.seat.id, view.seat.vm_name


# --------------------------------------------------------------------------
# command execution + streaming
# --------------------------------------------------------------------------
def test_command_exec_streams_output_and_finishes(manager, fake):
    fake.run_chunks = [
        (0.0, "stdout", "line-1\n"),
        (0.02, "stderr", "warn-1\n"),
        (0.02, "stdout", "line-2\n"),
    ]
    fake.run_exit_code = 0
    seat_id, vm_ref = _ready_seat(manager)

    view = manager.exec_start(seat_id, "e1", label="build", command="make test", timeout_s=10)
    assert view.state == "running"
    assert view.command == "make test"
    assert manager.seat_status(manager.queue_view()[0].id).seat.state == "busy"

    assert _wait_until(lambda: manager.exec_poll(seat_id, "e1").state == "finished")
    done = manager.exec_poll(seat_id, "e1")
    assert done.exit_code == 0
    assert done.stdout == "line-1\nline-2\n"
    assert done.stderr == "warn-1\n"
    assert done.truncated is False
    assert (vm_ref, "make test") in fake.runs
    assert _wait_until(lambda: manager.exec_engine.active_workers() == 0)
    assert manager.seat_status(manager.queue_view()[0].id).seat.state == "ready"


def test_exec_without_command_is_bookkeeping_only(manager, fake):
    seat_id, _ = _ready_seat(manager)
    started = manager.exec_start(seat_id, "e1", label="manual")
    assert started.state == "running"
    assert started.command is None
    manager.exec_output(seat_id, "e1", stdout="partial\n")
    manager.exec_finish(seat_id, "e1", exit_code=0, stdout="tail\n")
    view = manager.exec_poll(seat_id, "e1")
    assert view.state == "finished"
    assert view.exit_code == 0
    assert view.stdout == "partial\ntail\n"
    assert fake.runs == []  # no worker, no provisioner call
    assert manager.exec_engine.active_workers() == 0


# --------------------------------------------------------------------------
# seat-state rules
# --------------------------------------------------------------------------
def _insert_seat(store, name: str, state: str, vm_name: str | None) -> int:
    now = st.fmt_time(st.utcnow())
    with store.transaction() as conn:
        return st.insert_seat(
            conn,
            name=name,
            seat_type="terminal",
            image="golden-term",
            state=state,
            vm_name=vm_name,
            now=now,
        )


def test_exec_rejected_for_unknown_seat(manager):
    with pytest.raises(KeyError):
        manager.exec_start(999_999, "e1", command="echo hi")


def test_exec_rejected_without_vm(manager):
    seat_id = _insert_seat(manager.store, "terminal-bare", st.SeatState.READY.value, None)
    with pytest.raises(ExecNotAllowed) as excinfo:
        manager.exec_start(seat_id, "e1", command="echo hi")
    assert excinfo.value.code == "exec_not_allowed"


@pytest.mark.parametrize("state", ["provisioning", "resetting", "error", "off", "held"])
def test_exec_rejected_in_non_executable_state(manager, state):
    seat_id = _insert_seat(manager.store, f"terminal-{state}", state, "fake://x")
    with pytest.raises(ExecNotAllowed) as excinfo:
        manager.exec_start(seat_id, "e1", command="echo hi")
    assert excinfo.value.code == "exec_not_allowed"


def test_exec_rejected_when_vm_not_running(manager, fake):
    seat_id, vm_ref = _ready_seat(manager)
    fake.set_vm_state(vm_ref, "shut off")
    with pytest.raises(ExecNotAllowed):
        manager.exec_start(seat_id, "e1", command="echo hi")


# --------------------------------------------------------------------------
# concurrency cap / kill / timeout
# --------------------------------------------------------------------------
def test_concurrency_cap_enforced_for_command_execs(make_manager, fake):
    cfg = Config.default()
    cfg.exec.max_concurrent_per_seat = 1
    fake.run_hang_s = 5.0
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat_id, _ = _ready_seat(mgr)

    first = mgr.exec_start(seat_id, "e1", command="sleep 5", timeout_s=30)
    assert first.state == "running"
    with pytest.raises(ValueError, match="running execs"):
        mgr.exec_start(seat_id, "e2", command="sleep 5", timeout_s=30)
    mgr.exec_kill(seat_id, "e1")
    assert _wait_until(lambda: mgr.exec_engine.active_workers() == 0)


def test_exec_kill_stops_worker_and_returns_seat_ready(manager, fake):
    fake.run_hang_s = 5.0
    seat_id, _ = _ready_seat(manager)
    manager.exec_start(seat_id, "e1", command="sleep 5", timeout_s=30)
    assert manager.exec_engine.active_workers() == 1

    killed = manager.exec_kill(seat_id, "e1", signal=9)
    assert killed.state == "killed"
    assert killed.exit_code == -9
    assert _wait_until(lambda: manager.exec_engine.active_workers() == 0, timeout=2)
    assert _wait_until(
        lambda: manager.seat_status(manager.queue_view()[0].id).seat.state == "ready"
    )


def test_exec_wall_clock_timeout(make_manager, fake):
    cfg = Config.default()
    fake.run_chunks = [(0.0, "stdout", "partial\n")]
    fake.run_hang_s = 5.0
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat_id, _ = _ready_seat(mgr)

    started = time.monotonic()
    mgr.exec_start(seat_id, "e1", command="sleep 5", timeout_s=1)
    assert _wait_until(lambda: mgr.exec_poll(seat_id, "e1").state != "running", timeout=3)
    view = mgr.exec_poll(seat_id, "e1")
    assert view.exit_code == 124
    assert "timed out" in view.stderr
    assert view.stdout == "partial\n"
    assert time.monotonic() - started < 3


def test_timeout_is_clamped_to_max_runtime(make_manager, fake):
    cfg = Config.default()
    cfg.exec.max_runtime_s = 30
    fake.run_hang_s = 0.0
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat_id, _ = _ready_seat(mgr)
    view = mgr.exec_start(seat_id, "e1", command="true", timeout_s=9999)
    assert view.timeout_s == 30


def test_max_runtime_zero_means_no_engine_ceiling(make_manager, fake, monkeypatch):
    """Default 0 disables the engine-wide timer; no timeout reaches the transport."""
    cfg = Config.default()
    assert cfg.exec.max_runtime_s == 0
    fake.run_hang_s = 0.0
    seen: dict[str, int] = {}
    original = fake.run

    def spy(vm_ref, command, *, timeout_s=60, **kwargs):
        seen["timeout_s"] = timeout_s
        return original(vm_ref, command, timeout_s=timeout_s, **kwargs)

    monkeypatch.setattr(fake, "run", spy)
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat_id, _ = _ready_seat(mgr)

    view = mgr.exec_start(seat_id, "e1", command="make test", timeout_s=None)
    assert view.timeout_s == 0
    assert _wait_until(lambda: mgr.exec_poll(seat_id, "e1").state == "finished")
    assert seen["timeout_s"] == 0


def test_max_runtime_zero_still_honours_explicit_timeout(make_manager, fake):
    """With no engine ceiling, an explicit per-call timeout is still enforced."""
    cfg = Config.default()
    assert cfg.exec.max_runtime_s == 0
    fake.run_hang_s = 5.0
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat_id, _ = _ready_seat(mgr)

    started = time.monotonic()
    view = mgr.exec_start(seat_id, "e1", command="sleep 5", timeout_s=1)
    assert view.timeout_s == 1
    assert _wait_until(lambda: mgr.exec_poll(seat_id, "e1").state != "running", timeout=3)
    assert mgr.exec_poll(seat_id, "e1").exit_code == 124
    assert time.monotonic() - started < 3


# --------------------------------------------------------------------------
# a long exec must never block lifecycle work
# --------------------------------------------------------------------------
def test_long_exec_does_not_block_release(manager, fake):
    fake.run_hang_s = 10.0
    seat_id, vm_ref = _ready_seat(manager)
    manager.exec_start(seat_id, "e1", command="sleep 100", timeout_s=60)
    assert manager.exec_engine.active_workers() == 1

    started = time.monotonic()
    outcome = manager.release_seat(seat_id, export=False).result(timeout=5)
    assert outcome.destroyed is True
    assert time.monotonic() - started < 3
    assert fake.destroyed == [vm_ref]
    # The worker observed cancel and unwound on its own thread.
    assert _wait_until(lambda: manager.exec_engine.active_workers() == 0, timeout=2)
    assert manager.exec_poll(seat_id, "e1").state == "killed"


def test_long_exec_does_not_block_reset(manager, fake):
    fake.run_hang_s = 10.0
    seat_id, vm_ref = _ready_seat(manager)
    manager.exec_start(seat_id, "e1", command="sleep 100", timeout_s=60)

    started = time.monotonic()
    view = manager.reset_seat(seat_id).result(timeout=5)
    assert view.state == "ready"
    assert time.monotonic() - started < 3
    assert fake.resets == [vm_ref]
    assert manager.exec_poll(seat_id, "e1").state == "killed"


def test_long_exec_does_not_block_admission(make_manager, fake):
    cfg = Config.default()
    fake.run_hang_s = 10.0
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    terminal_id, _ = _ready_seat(mgr, "term-agent", "terminal")
    mgr.exec_start(terminal_id, "e1", command="sleep 100", timeout_s=60)

    # A fresh desktop request must still be admitted and provisioned while the
    # terminal exec is running.
    desktop = mgr.request_seat("desk-agent", "desktop")
    view = desktop.wait_ready(timeout=5)
    assert view.seat.state == "ready"
    assert mgr.exec_poll(terminal_id, "e1").state == "running"


# --------------------------------------------------------------------------
# libvirt streaming transport (no libvirt, no VM: a plain host subprocess)
# --------------------------------------------------------------------------
def test_subprocess_stream_runner_forwards_output():
    chunks: list[tuple[str, str]] = []
    result = _subprocess_stream_runner(
        [sys.executable, "-c", "print('one'); print('two')"],
        10,
        lambda stream, text: chunks.append((stream, text)),
        None,
    )
    assert result.returncode == 0
    assert "".join(text for _, text in chunks) == "one\ntwo\n"
    assert all(stream == "stdout" for stream, _ in chunks)


def test_subprocess_stream_runner_enforces_timeout():
    started = time.monotonic()
    result = _subprocess_stream_runner(
        [sys.executable, "-c", "import time; time.sleep(30)"], 1, None, None
    )
    assert result.returncode == 124
    assert time.monotonic() - started < 5


def test_subprocess_stream_runner_honours_cancel():
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        result = _subprocess_stream_runner(
            [sys.executable, "-c", "import time; time.sleep(30)"], 30, None, cancel
        )
        assert result.returncode != 0
        assert time.monotonic() - started < 5
    finally:
        timer.cancel()


def test_fake_provisioner_run_is_cancellable():
    fake = FakeProvisioner(run_hang_s=30)
    vm_ref = fake.inject_vm("x")
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        result = fake.run(vm_ref, "sleep 30", timeout_s=30, cancel=cancel)
        assert result.returncode == -9
    finally:
        timer.cancel()
