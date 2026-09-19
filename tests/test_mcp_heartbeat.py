"""MCP server auto-heartbeat + stasis regression (FakeProvisioner, no VMs).

The MCP server is the agent's proxy: while it holds a seat it must beat on the
agent's behalf. A heartbeat timeout can then only mean the MCP server process
itself died -- and when that happens the seat enters stasis (``held``) rather
than being destroyed.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from omavroom.client import DaemonClient
from omavroom.config import Config
from omavroom.daemon import DaemonServer
from omavroom.manager import Manager
from omavroom.manager.provisioner import FakeProvisioner
from omavroom.mcp.server import OmavroomTools


def _wait_for_socket(path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"daemon socket never appeared: {path}")
        time.sleep(0.01)


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


@pytest.fixture
def leased_env(tmp_path):
    """In-process daemon with a 1s heartbeat timeout and a fast MCP beat."""
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    cfg.leases.heartbeat_interval_s = 1
    cfg.leases.heartbeat_timeout_s = 1
    cfg.leases.lease_timeout_s = 3600
    cfg.leases.held_ttl_s = 0
    fake = FakeProvisioner()
    manager = Manager(
        cfg, db_path=tmp_path / "state.db", provisioner=fake, free_ram_mb=lambda: 10**9
    )
    socket_path = tmp_path / "run" / "daemon.sock"
    server = DaemonServer(manager, socket_path=socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_socket(socket_path)
    client = DaemonClient(socket_path=socket_path)
    tools = OmavroomTools(client, heartbeat_interval_s=0.05, heartbeat_timeout_s=1.0)
    env = SimpleNamespace(
        cfg=cfg,
        fake=fake,
        manager=manager,
        server=server,
        client=client,
        tools=tools,
        socket_path=socket_path,
        thread=thread,
    )
    try:
        yield env
    finally:
        tools.stop()
        client.close()
        server.shutdown()
        thread.join(timeout=5)


def _ready(env, label: str = "agent-1") -> int:
    request = env.tools.request_seat(label, "terminal")
    view = env.tools.wait_for_seat(request["request_id"], timeout_s=5)
    return int(view["seat"]["id"])


def _seat(env, seat_id: int):
    return next(s for s in env.manager.list_seats() if s.id == seat_id)


def _heartbeat_count(env, seat_id: int | None = None) -> int:
    return sum(
        1
        for event in env.manager.list_events(limit=2000)
        if event.event_type == "heartbeat" and (seat_id is None or event.seat_id == seat_id)
    )


def test_auto_heartbeat_is_sent_over_time(leased_env):
    seat_id = _ready(leased_env)
    before = _heartbeat_count(leased_env, seat_id)

    time.sleep(1.5)  # > heartbeat_timeout_s, with no explicit heartbeat call

    after = _heartbeat_count(leased_env, seat_id)
    assert after > before, "the MCP server did not beat the seat on the agent's behalf"
    assert seat_id in leased_env.tools.heartbeats.tracked()


def test_seat_survives_while_mcp_server_is_alive(leased_env):
    seat_id = _ready(leased_env)

    time.sleep(2.0)  # well past heartbeat_timeout_s

    view = _seat(leased_env, seat_id)
    assert view.state in ("ready", "busy")
    assert leased_env.fake.destroyed == []
    assert seat_id not in leased_env.manager.pool_status().needs_attention


def test_tool_calls_keep_working(leased_env):
    seat_id = _ready(leased_env)
    assert leased_env.tools.list_seats()
    assert leased_env.tools.list_events(limit=5)
    result = leased_env.tools.exec_run(seat_id, "echo hi", timeout_s=5)
    assert result["exit_code"] == 0


def test_beating_stops_after_release(leased_env):
    seat_id = _ready(leased_env)
    handle = leased_env.tools.release_seat(seat_id, export=False)
    leased_env.tools.job_wait(handle["job_id"], timeout_s=5)

    assert _wait_until(lambda: seat_id not in leased_env.tools.heartbeats.tracked())
    settled = _heartbeat_count(leased_env, seat_id)
    time.sleep(0.3)
    assert _heartbeat_count(leased_env, seat_id) == settled


def test_beating_stops_after_force_discard(leased_env):
    seat_id = _ready(leased_env)
    handle = leased_env.tools.force_discard(seat_id, reason="test")
    leased_env.tools.job_wait(handle["job_id"], timeout_s=5)

    assert seat_id not in leased_env.tools.heartbeats.tracked()
    settled = _heartbeat_count(leased_env, seat_id)
    time.sleep(0.3)
    assert _heartbeat_count(leased_env, seat_id) == settled
    assert leased_env.fake.destroyed


def test_stasis_when_beating_stops(leased_env):
    seat_id = _ready(leased_env)
    vm_ref = _seat(leased_env, seat_id).vm_name
    leased_env.tools.stop()  # simulate the MCP server process dying

    assert _wait_until(lambda: _seat(leased_env, seat_id).state == "held", timeout=6)

    view = _seat(leased_env, seat_id)
    assert view.vm_name == vm_ref  # VM + overlay preserved, not destroyed
    assert leased_env.fake.destroyed == []
    assert view.last_error == "stale: heartbeat_timeout"
    assert seat_id in leased_env.manager.pool_status().needs_attention

    forced = leased_env.manager.force_discard(seat_id, reason="cleanup").result(timeout=5)
    assert forced.state == "off"
