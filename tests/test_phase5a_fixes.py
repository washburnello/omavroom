"""Phase 5A round-2 regression tests (G1, G2, A1-A4).

These tests are written to fail against the pre-fix code and pass after the
round-2 fixes. Everything runs in-process with FakeProvisioner; no VM boots.
"""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import pytest

from omavroom import state as st
from omavroom.client import DaemonClient
from omavroom.config import Config
from omavroom.daemon import DaemonServer
from omavroom.manager import Manager
from omavroom.manager.exec_engine import ExecEngine
from omavroom.manager.execs import ExecTracker
from omavroom.manager.libvirt_provisioner import _subprocess_stream_runner
from omavroom.manager.provisioner import FakeProvisioner, InputEvent

#: Matches ExecTracker's planned default aggregate list-output budget.
LIST_OUTPUT_BUDGET_BYTES = 8192


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


# ==========================================================================
# G1 - streaming transport must not retain unbounded output
# ==========================================================================
def test_stream_runner_does_not_retain_unbounded_output():
    seen: list[int] = []
    script = "import sys\nfor _ in range(2000):\n    sys.stdout.write('x' * 500)\n"
    result = _subprocess_stream_runner(
        [sys.executable, "-c", script],
        30,
        lambda stream, text: seen.append(len(text)),
        None,
    )
    assert result.returncode == 0
    assert sum(seen) >= 1_000_000  # everything was forwarded to the callback
    # In streaming mode the transport must not keep its own copy.
    assert result.stdout == ""
    assert result.stderr == ""


def test_engine_ring_is_bounded_and_truncated(make_manager, fake):
    cfg = Config.default()
    cfg.exec.max_output_bytes = 1000
    fake.run_chunks = [(0.0, "stdout", "x" * 400)] * 5  # 2000 chars > cap
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat_id, _ = _ready_seat(mgr)
    mgr.exec_start(seat_id, "e1", command="big", timeout_s=10)
    assert _wait_until(lambda: mgr.exec_poll(seat_id, "e1").state != "running")
    view = mgr.exec_poll(seat_id, "e1")
    assert len(view.stdout) <= 1000
    assert view.truncated is True


# ==========================================================================
# G2 - kill/cancel must not be lost between start() and worker spawn
# ==========================================================================
def test_kill_between_start_and_spawn_never_runs(manager, fake):
    fake.run_hang_s = 5.0
    seat_id, _ = _ready_seat(manager)

    in_on_busy = threading.Event()
    release_on_busy = threading.Event()
    original = manager.exec_engine._on_busy

    def slow_on_busy(sid: int) -> None:
        in_on_busy.set()
        release_on_busy.wait(3)
        original(sid)

    manager.exec_engine._on_busy = slow_on_busy

    start_errors: list[BaseException] = []
    started = threading.Event()

    def do_start() -> None:
        started.set()
        try:
            manager.exec_start(seat_id, "e1", command="sleep 5", timeout_s=30)
        except BaseException as exc:  # noqa: BLE001
            start_errors.append(exc)

    starter = threading.Thread(target=do_start)
    starter.start()
    assert in_on_busy.wait(2)

    killed: dict = {}

    def do_kill() -> None:
        killed["view"] = manager.exec_kill(seat_id, "e1")

    killer = threading.Thread(target=do_kill)
    killer.start()
    time.sleep(0.1)  # let the kill land while on_busy is still blocked
    release_on_busy.set()
    starter.join(3)
    killer.join(3)

    assert start_errors == []
    assert killed["view"].state == "killed"
    # The command must never reach the provisioner.
    assert "run" not in fake.call_names()
    assert fake.runs == []
    assert _wait_until(lambda: manager.exec_engine.active_workers() == 0)


def test_release_cancels_exec_before_spawn(manager, fake):
    fake.run_hang_s = 5.0
    seat_id, _ = _ready_seat(manager)

    in_on_busy = threading.Event()
    release_on_busy = threading.Event()
    original = manager.exec_engine._on_busy

    def slow_on_busy(sid: int) -> None:
        in_on_busy.set()
        release_on_busy.wait(3)
        original(sid)

    manager.exec_engine._on_busy = slow_on_busy

    def do_start() -> None:
        manager.exec_start(seat_id, "e1", command="sleep 5", timeout_s=30)

    starter = threading.Thread(target=do_start)
    starter.start()
    assert in_on_busy.wait(2)

    released: dict = {}

    def do_release() -> None:
        released["outcome"] = manager.release_seat(seat_id, export=False).result(timeout=5)

    releaser = threading.Thread(target=do_release)
    releaser.start()
    time.sleep(0.1)  # let cancel_seat land while on_busy is still blocked
    release_on_busy.set()
    starter.join(3)
    releaser.join(5)

    assert released["outcome"].destroyed is True
    assert "run" not in fake.call_names()
    assert fake.runs == []


def test_kill_in_tracker_start_window_never_runs(manager, fake):
    """G3: kill between tracker.start() and worker insertion must win.

    The record is marked KILLED before the worker entry is visible to
    ``_signal_worker``, so the cancel event is never set; ``_launch`` must
    consult the tracker's terminal state (or the worker's own pre-run guard)
    and refuse to execute.
    """
    fake.run_hang_s = 5.0
    seat_id, _ = _ready_seat(manager)
    tracker = manager.exec_engine.tracker

    in_window = threading.Event()
    proceed = threading.Event()
    original_start = tracker.start

    def slow_start(*args, **kwargs):
        view = original_start(*args, **kwargs)
        in_window.set()
        proceed.wait(3)
        return view

    tracker.start = slow_start  # type: ignore[assignment]

    start_errors: list[BaseException] = []

    def do_start() -> None:
        try:
            manager.exec_start(seat_id, "e1", command="sleep 5", timeout_s=30)
        except BaseException as exc:  # noqa: BLE001
            start_errors.append(exc)

    starter = threading.Thread(target=do_start)
    starter.start()
    assert in_window.wait(2)

    killed: dict = {}

    def do_kill() -> None:
        try:
            killed["view"] = manager.exec_kill(seat_id, "e1")
        except BaseException as exc:  # noqa: BLE001
            killed["error"] = exc

    killer = threading.Thread(target=do_kill)
    killer.start()
    # Kill has marked the record terminal, but the worker was not registered
    # yet, so its cancel event was never set.
    assert _wait_until(lambda: tracker.poll(seat_id, "e1").state == "killed", timeout=2)
    proceed.set()
    starter.join(3)
    killer.join(3)

    assert start_errors == []
    assert killed.get("view") is not None and killed["view"].state == "killed"
    assert fake.runs == []
    assert "run" not in fake.call_names()
    assert _wait_until(lambda: manager.exec_engine.active_workers() == 0)


def test_run_worker_pre_run_guard_blocks_cancelled(manager, fake):
    """G3: the worker's own pre-run check must not start a cancelled command."""
    seat_id, vm_ref = _ready_seat(manager)
    engine = manager.exec_engine
    cancel = threading.Event()
    cancel.set()
    worker = {"cancel": cancel, "signal": [9], "thread": None}
    with engine._lock:
        engine._workers[(seat_id, "e1")] = worker
    engine.tracker.start(seat_id, "e1", command="sleep 5")

    engine._run_worker(seat_id, "e1", vm_ref, "sleep 5", 30, worker)

    assert fake.runs == []
    assert "run" not in fake.call_names()
    assert engine.tracker.poll(seat_id, "e1").state == "killed"
    # The worker entry is cleaned up and the seat returns to ready.
    assert (seat_id, "e1") not in engine._workers
    assert _wait_until(lambda: manager.list_seats()[0].state == "ready")


# ==========================================================================
# A1 - OMAVROOM_MCP_AUTOSTART must be honored by main
# ==========================================================================
def _run_main_with_autostart(monkeypatch, argv, env_value=None):
    import omavroom.mcp.server as srv

    if env_value is None:
        monkeypatch.delenv("OMAVROOM_MCP_AUTOSTART", raising=False)
    else:
        monkeypatch.setenv("OMAVROOM_MCP_AUTOSTART", env_value)

    seen: dict = {}

    def fake_ensure(socket_path, *, autostart, **kwargs):
        seen["autostart"] = autostart
        raise srv.MCPDaemonError("stop here")

    monkeypatch.setattr(srv, "ensure_daemon_client", fake_ensure)
    assert srv.main(argv) == 1
    return seen["autostart"]


def test_main_honors_autostart_env(monkeypatch):
    # No explicit flag -> defer to the env (None), not a hard True.
    assert _run_main_with_autostart(monkeypatch, [], env_value="0") is None
    assert _run_main_with_autostart(monkeypatch, [], env_value="1") is None


def test_main_no_autostart_flag_wins(monkeypatch):
    assert _run_main_with_autostart(monkeypatch, ["--no-autostart"], env_value="1") is False


def test_ensure_daemon_client_honors_env(monkeypatch, tmp_path):
    from omavroom.mcp.server import MCPDaemonError, ensure_daemon_client

    monkeypatch.setenv("OMAVROOM_MCP_AUTOSTART", "0")
    with pytest.raises(MCPDaemonError, match="autostart is disabled"):
        ensure_daemon_client(tmp_path / "missing.sock", autostart=None)


# ==========================================================================
# A2 - bounded seat lock + non-starving MCP concurrency
# ==========================================================================
def test_desktop_op_seat_lock_times_out(make_manager, fake):
    cfg = Config.default()
    fake.run_hang_s = 0.0
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9, desktop_lock_timeout_s=0.2)
    seat_id, _ = _ready_seat(mgr, seat_type="desktop")
    seat = mgr.store.read(lambda c: st.seat_by_id(c, seat_id))

    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with mgr.locks.seat(seat.name):
            holding.set()
            release.wait(3)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(2)
    try:
        started = time.monotonic()
        with pytest.raises(Exception) as excinfo:
            mgr.screenshot(seat_id)
        assert getattr(excinfo.value, "code", None) == "seat_busy"
        assert time.monotonic() - started < 1.5
    finally:
        release.set()
        holder.join(3)


def _daemon_env(tmp_path, *, desktop_lock_timeout_s: float = 0.3):
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    fake = FakeProvisioner()
    manager = Manager(
        cfg,
        db_path=tmp_path / "state.db",
        provisioner=fake,
        free_ram_mb=lambda: 10**9,
        desktop_lock_timeout_s=desktop_lock_timeout_s,
    )
    socket_path = tmp_path / "run" / "daemon.sock"
    server = DaemonServer(manager, socket_path=socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    client = DaemonClient(socket_path=socket_path)
    env = SimpleNamespace(
        fake=fake, manager=manager, server=server, client=client, socket_path=socket_path
    )
    try:
        yield env
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def daemon_env(tmp_path):
    yield from _daemon_env(tmp_path)


def test_mcp_screenshot_bounded_and_heartbeat_not_starved(daemon_env):
    from omavroom.client import DaemonRequestError
    from omavroom.mcp.server import OmavroomTools

    env = daemon_env
    env.fake.export_delay_s = 2.0
    seat = env.client.request_seat("agent-1", "desktop")
    view = seat.wait_ready(timeout=5)
    seat_id = view["seat"]["id"]

    # A long release holds the seat lock; the export delay keeps it held.
    env.client.release_seat(seat_id, repo="demo", branch="task")
    seat_name = env.manager.store.read(lambda c: st.seat_by_id(c, seat_id)).name
    assert _wait_until(lambda: env.manager.locks.seat_held(seat_name), timeout=3)

    # Post-fix the desktop op uses its own connection; pre-fix it shares the
    # client with heartbeat and starves it.
    tools = OmavroomTools(
        env.client, client_factory=lambda: DaemonClient(socket_path=env.socket_path)
    )

    shot_error: dict = {}

    def do_screenshot() -> None:
        try:
            tools.screenshot(seat_id)
        except BaseException as exc:  # noqa: BLE001
            shot_error["exc"] = exc

    shooter = threading.Thread(target=do_screenshot)
    shooter.start()

    started = time.monotonic()
    env.client.heartbeat(seat_id=seat_id)
    heartbeat_dt = time.monotonic() - started
    # The export holds the seat for ~2 s; a heartbeat that shares the blocked
    # connection would take about that long. Generous margin still proves it
    # is not starved.
    assert heartbeat_dt < 1.5, f"heartbeat starved for {heartbeat_dt:.2f}s"

    shooter.join(3)
    assert isinstance(shot_error.get("exc"), DaemonRequestError)
    assert shot_error["exc"].code == "seat_busy"


# ==========================================================================
# A3 - bounded exec listing / no deep copy for state-only reads
# ==========================================================================
def test_list_execs_output_is_bounded(make_manager, fake):
    cfg = Config.default()
    cfg.exec.max_output_bytes = 100_000
    fake.run_chunks = [(0.0, "stdout", "x" * 100_000), (0.0, "stderr", "y" * 100_000)]
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    seat_id, _ = _ready_seat(mgr)
    mgr.exec_start(seat_id, "e1", command="big", timeout_s=10)
    assert _wait_until(lambda: mgr.exec_poll(seat_id, "e1").state != "running")

    bounded = mgr.list_execs(seat_id)
    total = sum(len(v.stdout) + len(v.stderr) for v in bounded)
    assert total <= LIST_OUTPUT_BUDGET_BYTES

    full = mgr.list_execs(seat_id, include_output=True, max_total_output_bytes=None)
    assert len(full[0].stdout) == 100_000

    metadata = mgr.list_execs(seat_id, include_output=False)
    assert metadata[0].stdout == "" and metadata[0].stderr == ""


def _run_one_big_exec(env, tags: int = 5000) -> int:
    env.fake.run_chunks = [(0.0, "stdout", "x" * tags)]
    seat = env.client.request_seat("agent-1", "terminal")
    seat_id = seat.wait_ready(timeout=5)["seat"]["id"]
    env.client.exec_start(seat_id, "e1", command="big", timeout_s=10)
    deadline = time.monotonic() + 5
    while env.client.exec_poll(seat_id, "e1")["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    return seat_id


def test_daemon_list_execs_options_plumbed(daemon_env):
    env = daemon_env
    seat_id = _run_one_big_exec(env)

    meta = env.client.call("list_execs", seat_id=seat_id, include_output=False)
    assert meta and all(r["stdout"] == "" and r["stderr"] == "" for r in meta)

    bounded = env.client.call(
        "list_execs", seat_id=seat_id, include_output=True, max_total_output_bytes=5
    )
    assert sum(len(r["stdout"]) + len(r["stderr"]) for r in bounded) <= 5

    full = env.client.list_execs(seat_id, include_output=True, max_total_output_bytes=None)
    assert len(full[0]["stdout"]) == 5000


def test_mcp_list_execs_options(daemon_env):
    from omavroom.mcp.server import OmavroomTools

    env = daemon_env
    seat_id = _run_one_big_exec(env)
    tools = OmavroomTools(env.client)

    meta = tools.list_execs(seat_id, include_output=False)
    assert meta and all(r["stdout"] == "" and r["stderr"] == "" for r in meta)

    bounded = tools.list_execs(seat_id, include_output=True, max_total_output_bytes=5)
    assert sum(len(r["stdout"]) + len(r["stderr"]) for r in bounded) <= 5


def test_tracker_running_exec_ids_are_cheap():
    tracker = ExecTracker(max_output_bytes=100_000)
    tracker.start(1, "e1", command="big")
    tracker.record_output(1, "e1", stdout="x" * 100_000)
    tracker.start(1, "e2", command="other")
    tracker.finish(1, "e2", exit_code=0)
    ids = tracker.running_exec_ids(1)
    assert ids == ["e1"]
    assert tracker.active_total() == 1
    # Metadata-only list carries no buffers.
    views = tracker.list(1, include_output=False)
    assert all(v.stdout == "" and v.stderr == "" for v in views)


# ==========================================================================
# A4 - robustness fixes
# ==========================================================================
def test_global_concurrent_exec_cap(make_manager, fake):
    cfg = Config.default()
    cfg.exec.max_concurrent_per_seat = 10
    cfg.exec.max_concurrent_total = 2
    fake.run_hang_s = 10.0
    mgr = make_manager(cfg, provisioner=fake, free_ram_mb=lambda: 10**9)
    a = mgr.request_seat("A", "terminal")
    a_view = a.wait_ready(timeout=5)
    b = mgr.request_seat("B", "terminal")
    b_view = b.wait_ready(timeout=5)

    mgr.exec_start(a_view.seat.id, "e1", command="sleep 10", timeout_s=30)
    mgr.exec_start(b_view.seat.id, "e1", command="sleep 10", timeout_s=30)
    with pytest.raises(ValueError, match="global"):
        mgr.exec_start(a_view.seat.id, "e2", command="sleep 10", timeout_s=30)
    mgr.exec_kill(a_view.seat.id, "e1")
    mgr.exec_kill(b_view.seat.id, "e1")


def test_config_parses_max_concurrent_total(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[exec]\nmax_concurrent_total = 7\n", encoding="utf-8")
    cfg = Config.from_toml(path)
    assert cfg.exec.max_concurrent_total == 7


def test_finish_work_cannot_override_a_new_exec(manager, fake):
    """Busy/idle inversion: a late idle must not mark a running seat ready."""
    seat_id, _ = _ready_seat(manager)
    manager.exec_start(seat_id, "e1")  # bookkeeping exec -> busy

    entered = threading.Event()
    release = threading.Event()
    original_finish = manager.scheduler.finish_work

    def slow_finish(sid: int):
        entered.set()
        release.wait(3)
        return original_finish(sid)

    manager.scheduler.finish_work = slow_finish

    finisher = threading.Thread(target=lambda: manager.exec_finish(seat_id, "e1", exit_code=0))
    finisher.start()
    assert entered.wait(2)

    starter = threading.Thread(target=lambda: manager.exec_start(seat_id, "e2"))
    starter.start()
    time.sleep(0.1)
    release.set()
    finisher.join(3)
    starter.join(3)

    seats = [s for s in manager.list_seats() if s.id == seat_id]
    assert seats and seats[0].state == "busy"


def test_shutdown_before_spawn_calls_on_idle():
    tracker = ExecTracker()
    fake = FakeProvisioner()
    vm_ref = fake.inject_vm("terminal-1", seat_type="terminal")
    idles: list[int] = []
    engine = ExecEngine(
        tracker,
        fake,
        read_seat=lambda sid: SimpleNamespace(vm_name=vm_ref, state="ready"),
        on_idle=idles.append,
    )
    engine._shutdown = True
    engine.start(1, "e1", command="true", timeout_s=5)
    assert idles == [1]
    assert tracker.active_count(1) == 0
    assert "run" not in fake.call_names()


def test_input_event_rejects_bad_value():
    with pytest.raises(ValueError):
        InputEvent("key", None)
    with pytest.raises(ValueError):
        InputEvent("key", "")


def test_daemon_input_requires_wellformed_events(daemon_env):
    from omavroom.client import DaemonRequestError

    seat = daemon_env.client.request_seat("agent-1", "desktop")
    seat_id = seat.wait_ready(timeout=5)["seat"]["id"]
    with pytest.raises(DaemonRequestError) as excinfo:
        daemon_env.client.call(
            "input", seat_id=seat_id, events=[{"kind": "teleport", "value": "x"}]
        )
    assert excinfo.value.code == "invalid"
    with pytest.raises(DaemonRequestError):
        daemon_env.client.call("input", seat_id=seat_id, events=[{"kind": "key"}])
    assert daemon_env.client.ping() is True


def test_mcp_input_validation_is_clear(daemon_env):
    from omavroom.mcp.server import OmavroomTools

    seat = daemon_env.client.request_seat("agent-1", "desktop")
    seat_id = seat.wait_ready(timeout=5)["seat"]["id"]
    tools = OmavroomTools(daemon_env.client)
    with pytest.raises(ValueError, match="object"):
        tools.input(seat_id, ["not-an-object"])
    with pytest.raises(ValueError, match="kind"):
        tools.input(seat_id, [{"kind": "teleport", "value": "x"}])
    with pytest.raises(ValueError, match="value"):
        tools.input(seat_id, [{"kind": "key"}])


def test_daemon_screenshot_byte_cap(daemon_env):
    env = daemon_env
    seat = env.client.request_seat("agent-1", "desktop")
    seat_id = seat.wait_ready(timeout=5)["seat"]["id"]
    result = env.client.call("screenshot", seat_id=seat_id, max_bytes=6)
    assert result["max_bytes"] == 6
    import base64

    assert len(base64.b64decode(result["png_base64"])) <= 6


def test_libvirt_screenshot_resizer_unavailable_raises(tmp_path):
    from pathlib import Path

    from omavroom.manager.libvirt_provisioner import (
        CommandResult,
        LibvirtProvisioner,
        _SeatMeta,
        _subprocess_runner,
    )
    from omavroom.manager.provisioner import ProvisionerError

    # A PNG already within the width cap but over the byte cap, with no
    # ImageMagick: the old code silently returned the oversized image.
    big_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000

    def runner(argv, timeout, input_text):
        if argv[0] == "virsh":
            if "screenshot" in argv:
                target = Path(argv[argv.index("--file") + 1])
                target.write_bytes(big_png)
            return CommandResult(0, "", "")
        return _subprocess_runner(argv, timeout, input_text)

    prov = LibvirtProvisioner(Config.default(), base_dir=tmp_path, host_runner=runner)
    prov.magick_bin = None
    overlay = tmp_path / "seats" / "desktop-1" / "overlay.qcow2"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"x")
    prov._save_meta(
        _SeatMeta(
            name="desktop-1",
            seat_type="desktop",
            image="golden-desktop",
            golden=str(overlay),
            mac="52:54:00:00:00:01",
            uuid="00000000-0000-0000-0000-000000000000",
            domain="omavroom-seat-desktop-1",
            overlay=str(overlay),
            nvram=str(overlay.parent / "VARS.fd"),
        )
    )
    with pytest.raises(ProvisionerError, match="ImageMagick|max_bytes|exceeds"):
        prov.screenshot("omavroom-seat-desktop-1", max_bytes=100)
