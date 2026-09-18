"""Phase 5 MCP server tests against an in-process daemon (FakeProvisioner).

No VM is booted: the daemon and the FastMCP server both run in-process on a
per-test temp socket, and the server is exercised both by calling its tool
methods directly and through FastMCP's in-memory client.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.utilities.types import Image

from omavroom.client import DaemonClient
from omavroom.config import Config
from omavroom.daemon import DaemonServer
from omavroom.manager import Manager
from omavroom.manager.provisioner import FakeProvisioner
from omavroom.mcp.server import (
    MCP_TOOL_NAMES,
    MCPDaemonError,
    OmavroomTools,
    build_server,
    ensure_daemon_client,
)


def _wait_for_socket(path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"daemon socket never appeared: {path}")
        time.sleep(0.01)


@pytest.fixture
def mcp_env(tmp_path):
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    fake = FakeProvisioner()
    db = tmp_path / "state.db"
    manager = Manager(cfg, db_path=db, provisioner=fake, free_ram_mb=lambda: 10**9)
    socket_path = tmp_path / "run" / "daemon.sock"
    server = DaemonServer(manager, socket_path=socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_socket(socket_path)
    client = DaemonClient(socket_path=socket_path)
    tools = OmavroomTools(client)
    env = SimpleNamespace(
        manager=manager,
        fake=fake,
        server=server,
        client=client,
        tools=tools,
        socket_path=socket_path,
        config=cfg,
        thread=thread,
    )
    try:
        yield env
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=5)


def _ready_seat(env, label: str = "agent-1") -> int:
    request = env.tools.request_seat(label, "terminal")
    view = env.tools.wait_for_seat(request["request_id"], timeout_s=5)
    return view["seat"]["id"]


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# --------------------------------------------------------------------------
# registration / transports
# --------------------------------------------------------------------------
def test_all_tools_registered_with_schemas(mcp_env):
    server = build_server(mcp_env.tools)

    async def main():
        async with Client(server) as client:
            return await client.list_tools()

    tools = asyncio.run(main())
    names = {tool.name for tool in tools}
    assert names == set(MCP_TOOL_NAMES)
    by_name = {tool.name: tool for tool in tools}
    schema = by_name["exec_run"].input_schema
    assert "seat_id" in schema["properties"]
    assert "command" in schema["properties"]
    assert schema["properties"]["timeout_s"]["default"] == 30


def test_tools_callable_through_in_memory_client(mcp_env):
    mcp_env.fake.run_chunks = [(0.0, "stdout", "hello\n")]
    seat_id = _ready_seat(mcp_env)
    server = build_server(mcp_env.tools)

    async def main():
        async with Client(server) as client:
            status = await client.call_tool("pool_status", {})
            exec_result = await client.call_tool(
                "exec_run", {"seat_id": seat_id, "command": "echo hello"}
            )
            shot = await client.call_tool("screenshot", {"seat_id": seat_id})
            return status, exec_result, shot

    status, exec_result, shot = asyncio.run(main())
    assert "per_type" in status.data
    assert exec_result.data["exit_code"] == 0
    assert "hello" in exec_result.data["stdout"]
    content_types = [getattr(block, "type", None) for block in shot.content]
    assert "image" in content_types
    assert "text" in content_types


# --------------------------------------------------------------------------
# seat lifecycle
# --------------------------------------------------------------------------
def test_request_seat_and_wait_for_seat(mcp_env):
    request = mcp_env.tools.request_seat("agent-x", "terminal", project="demo")
    assert request["status"] in ("waiting", "claimed")
    view = mcp_env.tools.wait_for_seat(request["request_id"], timeout_s=5)
    assert view["status"] == "claimed"
    assert view["seat"]["state"] == "ready"
    assert view["seat"]["vm_name"] == "fake://terminal-1"
    assert [s["id"] for s in mcp_env.tools.list_seats()] == [view["seat"]["id"]]
    lease = mcp_env.tools.heartbeat(seat_id=view["seat"]["id"])
    assert lease["seat_id"] == view["seat"]["id"]
    event_types = {e["event_type"] for e in mcp_env.tools.list_events(limit=50)}
    assert "seat_ready" in event_types


# --------------------------------------------------------------------------
# exec: exec_run (short, bounded) and exec_start/poll (long, async)
# --------------------------------------------------------------------------
def test_exec_run_returns_combined_output(mcp_env):
    mcp_env.fake.run_chunks = [
        (0.0, "stdout", "line-1\n"),
        (0.02, "stderr", "warn\n"),
        (0.02, "stdout", "line-2\n"),
    ]
    seat_id = _ready_seat(mcp_env)
    result = mcp_env.tools.exec_run(seat_id, "make test", timeout_s=5)
    assert result["timed_out"] is False
    assert result["state"] == "finished"
    assert result["exit_code"] == 0
    assert result["stdout"] == "line-1\nline-2\n"
    assert result["stderr"] == "warn\n"
    assert result["exec_id"].startswith("mcp-")


def test_exec_run_timeout_is_bounded_and_kills(mcp_env):
    mcp_env.fake.run_chunks = [(0.0, "stdout", "partial\n")]
    mcp_env.fake.run_hang_s = 30.0
    seat_id = _ready_seat(mcp_env)

    started = time.monotonic()
    result = mcp_env.tools.exec_run(seat_id, "sleep 30", timeout_s=1)
    assert time.monotonic() - started < 5
    assert result["timed_out"] is True
    assert result["state"] == "killed"
    assert result["stdout"] == "partial\n"
    assert _wait_until(lambda: mcp_env.manager.exec_engine.active_workers() == 0, timeout=2)


def test_exec_start_and_poll_long_command(mcp_env):
    mcp_env.fake.run_chunks = [
        (0.0, "stdout", "step-1\n"),
        (0.05, "stdout", "step-2\n"),
        (0.05, "stdout", "step-3\n"),
    ]
    seat_id = _ready_seat(mcp_env)
    started = mcp_env.tools.exec_start(seat_id, "long-build", label="build", timeout_s=10)
    assert started["state"] == "running"
    exec_id = started["exec_id"]

    assert _wait_until(lambda: mcp_env.tools.exec_poll(seat_id, exec_id)["state"] != "running")
    polled = mcp_env.tools.exec_poll(seat_id, exec_id)
    assert polled["exit_code"] == 0
    assert polled["stdout"] == "step-1\nstep-2\nstep-3\n"


def test_exec_kill_tool(mcp_env):
    mcp_env.fake.run_hang_s = 30.0
    seat_id = _ready_seat(mcp_env)
    started = mcp_env.tools.exec_start(seat_id, "sleep 30", timeout_s=30)
    killed = mcp_env.tools.exec_kill(seat_id, started["exec_id"])
    assert killed["state"] == "killed"
    assert killed["exit_code"] == -9


# --------------------------------------------------------------------------
# desktop + peek
# --------------------------------------------------------------------------
def test_screenshot_returns_image_and_base64(mcp_env):
    seat_id = _ready_seat(mcp_env)
    result = mcp_env.tools.screenshot(seat_id, max_width=320)
    assert isinstance(result[0], Image)
    assert result[0].data.startswith(b"PNG:")
    import base64

    assert base64.b64decode(result[1]) == result[0].data


def test_input_and_peek_aliases(mcp_env):
    seat_id = _ready_seat(mcp_env)
    applied = mcp_env.tools.input(
        seat_id, [{"kind": "text", "value": "hello"}, {"kind": "key", "value": "Return"}]
    )
    assert applied["applied"] == 2
    url = mcp_env.tools.peek_url(seat_id)
    assert url == mcp_env.tools.peek_endpoint(seat_id)
    assert url == mcp_env.tools.peek_attach(seat_id)
    assert url["endpoint"].startswith("vnc://")


# --------------------------------------------------------------------------
# long ops return jobs
# --------------------------------------------------------------------------
def test_release_seat_job_and_job_wait(mcp_env):
    seat_id = _ready_seat(mcp_env)
    handle = mcp_env.tools.release_seat(seat_id, export=False)
    assert "job_id" in handle
    result = mcp_env.tools.job_wait(handle["job_id"], timeout_s=5)
    assert result["state"] == "done"
    assert result["result"]["destroyed"] is True
    assert mcp_env.tools.list_seats() == []


def test_reconcile_returns_job(mcp_env):
    handle = mcp_env.tools.reconcile()
    result = mcp_env.tools.job_wait(handle["job_id"], timeout_s=5)
    assert result["state"] == "done"


# --------------------------------------------------------------------------
# daemon reuse / autostart guard
# --------------------------------------------------------------------------
def test_ensure_daemon_client_reuses_live_daemon(mcp_env):
    reused = ensure_daemon_client(mcp_env.socket_path, autostart=False)
    try:
        assert reused.ping() is True
    finally:
        reused.close()


def test_ensure_daemon_client_no_autostart_raises(tmp_path):
    with pytest.raises(MCPDaemonError, match="autostart is disabled"):
        ensure_daemon_client(tmp_path / "missing.sock", autostart=False)


def test_ensure_daemon_client_autostarts_detached(tmp_path, monkeypatch):
    import sys

    import omavroom.mcp.server as srv

    recorded: dict = {}

    class FakeProc:
        returncode = 0

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return FakeProc()

    calls = {"n": 0}

    def fake_live(path, timeout=0.5):
        calls["n"] += 1
        return calls["n"] > 1  # not live before start, live afterwards

    monkeypatch.setattr(srv, "socket_is_live", fake_live)
    monkeypatch.setattr(srv.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(DaemonClient, "ping", lambda self: True)

    sock = tmp_path / "run" / "daemon.sock"
    client = ensure_daemon_client(sock, autostart=True, start_timeout=2, provisioner="fake")
    try:
        assert client.ping() is True
    finally:
        client.close()

    argv = recorded["argv"]
    assert argv[:3] == [sys.executable, "-m", "omavroom.daemon"]
    assert "--socket" in argv and str(sock) in argv
    assert "--provisioner" in argv and "fake" in argv
    assert recorded["kwargs"]["start_new_session"] is True
