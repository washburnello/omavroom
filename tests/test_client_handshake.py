"""MCP client version handshake + typed stale-heartbeat error (no VMs).

The daemon records each client's ``hello`` (version + capabilities) and flags a
client that lacks ``auto_heartbeat`` as stale -- an opencode process that was
not restarted after upgrading omavroom. A heartbeat on a dead/absent lease must
answer with the clean ``not_found`` code, never a bare ``KeyError``.
"""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from omavroom.client import DaemonClient, DaemonRequestError
from omavroom.config import Config
from omavroom.daemon import PROTOCOL_VERSION, DaemonServer
from omavroom.manager import LeaseNotFound, Manager
from omavroom.manager.provisioner import FakeProvisioner
from omavroom.version import (
    CAPABILITY_AUTO_HEARTBEAT,
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    SERVER_NAME,
)


def _wait_for_socket(path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"daemon socket never appeared: {path}")
        time.sleep(0.01)


@pytest.fixture
def daemon_env(tmp_path):
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
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
    env = SimpleNamespace(
        manager=manager, server=server, client=client, socket_path=socket_path, config=cfg
    )
    try:
        yield env
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=5)


def _ready_desktop(env) -> int:
    seat = env.client.request_seat("agent-1", "desktop")
    return seat.wait_ready(timeout=5)["seat"]["id"]


# --------------------------------------------------------------------------
# hello handshake
# --------------------------------------------------------------------------
def test_hello_returns_protocol_and_version(daemon_env):
    reply = daemon_env.client.hello()
    assert reply["protocol"] == PROTOCOL_VERSION
    assert reply["server"] == SERVER_NAME
    assert reply["server_version"]
    assert CAPABILITY_AUTO_HEARTBEAT in reply["capabilities"]


def test_hello_records_client(daemon_env):
    daemon_env.client.hello()
    clients = daemon_env.manager.client_info()
    assert len(clients) == 1
    assert clients[0]["client"] == MCP_CLIENT_NAME
    assert clients[0]["version"] == MCP_CLIENT_VERSION
    assert clients[0]["auto_heartbeat"] is True
    assert daemon_env.manager.stale_clients() == []
    assert daemon_env.client.pool_status()["stale_clients"] == []


def test_hello_flags_and_warns_for_stale_client(daemon_env, caplog):
    with caplog.at_level(logging.WARNING, logger="omavroom.daemon"):
        reply = daemon_env.client.hello(client="omavroom-mcp", version="0.9", capabilities=[])
    assert reply["protocol"] == PROTOCOL_VERSION
    stale = daemon_env.manager.stale_clients()
    assert [c["client"] for c in stale] == ["omavroom-mcp"]
    assert stale[0]["auto_heartbeat"] is False
    # The daemon logs a clear warning so the operator knows to restart opencode.
    assert any("un-restarted opencode" in record.getMessage() for record in caplog.records)
    # And it is surfaced on the read view the CLI renders.
    assert daemon_env.client.pool_status()["stale_clients"] == stale


def test_hello_is_silent_for_current_client(daemon_env, caplog):
    with caplog.at_level(logging.WARNING, logger="omavroom.daemon"):
        daemon_env.client.hello()
    assert daemon_env.manager.stale_clients() == []
    assert not any("opencode" in record.getMessage() for record in caplog.records)


def test_status_report_shows_stale_client_restart_hint(daemon_env):
    from omavroom.cli.format import status_report

    daemon_env.client.hello(client="omavroom-mcp", version="0.9", capabilities=[])
    report = status_report(daemon_env.client.pool_status())
    assert "STALE MCP CLIENTS" in report
    assert "restart opencode" in report


def test_ping_includes_client_identity(daemon_env):
    from omavroom.mcp.server import OmavroomTools

    tools = OmavroomTools(daemon_env.client)
    try:
        reply = tools.ping()
        assert reply["client"] == MCP_CLIENT_NAME
        assert reply["client_version"] == MCP_CLIENT_VERSION
        assert CAPABILITY_AUTO_HEARTBEAT in reply["capabilities"]
    finally:
        tools.stop()


# --------------------------------------------------------------------------
# typed stale-heartbeat error
# --------------------------------------------------------------------------
def test_heartbeat_on_absent_lease_is_not_found(daemon_env):
    with pytest.raises(DaemonRequestError) as excinfo:
        daemon_env.client.heartbeat(seat_id=999999)
    assert excinfo.value.code == "not_found"


def test_heartbeat_on_closed_lease_is_not_found(daemon_env):
    seat_id = _ready_desktop(daemon_env)
    handle = daemon_env.client.release_seat(seat_id, export=False)
    handle.wait(timeout=5)
    # The lease is closed; a heartbeat naming the dead seat must be typed.
    with pytest.raises(DaemonRequestError) as excinfo:
        daemon_env.client.heartbeat(seat_id=seat_id)
    assert excinfo.value.code == "not_found"


def test_scheduler_raises_typed_error(daemon_env):
    with pytest.raises(LeaseNotFound):
        daemon_env.manager.scheduler.heartbeat(seat_id=999999)
