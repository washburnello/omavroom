"""Phase 4B2 daemon + client tests (no VMs; FakeProvisioner only).

The daemon runs in-process on a per-test temp socket with a FakeProvisioner,
so nothing here boots a VM or touches the production state/runtime dirs.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from omavroom import state as st
from omavroom.client import (
    DaemonAlreadyRunning as ClientAlreadyRunning,
)
from omavroom.client import (
    DaemonClient,
    DaemonNotRunning,
    DaemonRequestError,
    DaemonTimeout,
)
from omavroom.config import Config
from omavroom.daemon import (
    DEFAULT_SCREENSHOT_MAX_WIDTH,
    MAX_SCREENSHOT_MAX_WIDTH,
    PROTOCOL_VERSION,
    DaemonAlreadyRunning,
    DaemonServer,
    build_provisioner,
    configure_logging,
)
from omavroom.manager import Manager
from omavroom.manager.provisioner import FakeProvisioner, InputEvent, RepoSpec


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
    cfg.seats["terminal"].max_seats = 2
    cfg.seats["desktop"].max_seats = 1
    fake = FakeProvisioner()
    db = tmp_path / "state.db"
    manager = Manager(cfg, db_path=db, provisioner=fake, free_ram_mb=lambda: 10**9)
    socket_path = tmp_path / "run" / "daemon.sock"
    server = DaemonServer(manager, socket_path=socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_socket(socket_path)
    client = DaemonClient(socket_path=socket_path)
    env = SimpleNamespace(
        server=server,
        manager=manager,
        fake=fake,
        client=client,
        socket_path=socket_path,
        db=db,
        config=cfg,
        thread=thread,
    )
    try:
        yield env
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=5)


def _ready_seat(client, label: str = "agent-1"):
    request = client.request_seat(label, "terminal")
    view = request.wait_ready(timeout=5)
    seat_id = view["seat"]["id"]
    return request, seat_id


# --------------------------------------------------------------------------
# protocol / API surface
# --------------------------------------------------------------------------
def test_full_api_roundtrip(daemon_env):
    client = daemon_env.client
    assert client.ping() is True

    request, seat_id = _ready_seat(client)
    assert request.status()["status"] == "claimed"

    # reads
    status = client.pool_status()
    assert set(status["per_type"]) == {"desktop", "terminal"}
    assert [r["id"] for r in client.queue_view()] == [request.request_id]
    assert client.seat_status(request.request_id)["id"] == request.request_id
    assert [s["id"] for s in client.list_seats()] == [seat_id]
    assert client.list_events(limit=5)
    assert client.list_execs(seat_id) == []

    # lease + work
    lease = client.heartbeat(seat_id=seat_id)
    assert lease["seat_id"] == seat_id
    assert client.begin_work(seat_id)["state"] == "busy"
    assert client.finish_work(seat_id)["state"] == "ready"

    # exec tracking
    assert client.exec_start(seat_id, "e1", label="build")["state"] == "running"
    assert client.seat_status(request.request_id)["seat"]["state"] == "busy"
    assert client.exec_poll(seat_id, "e1")["state"] == "running"
    assert client.exec_finish(seat_id, "e1", exit_code=0)["state"] == "finished"
    assert client.exec_start(seat_id, "e2")["state"] == "running"
    assert client.exec_kill(seat_id, "e2")["state"] == "killed"
    assert {e["exec_id"] for e in client.list_execs(seat_id)} == {"e1", "e2"}

    # reset (long op) rewinds the seat and keeps it
    reset = client.reset_seat(seat_id).result(timeout=5)
    assert reset["state"] == "ready"
    assert daemon_env.fake.resets == ["fake://terminal-1"]

    # desktop ops
    png = client.screenshot(seat_id, max_width=160)
    assert png.startswith(b"PNG:")
    budget = client.input(seat_id, [InputEvent(kind="key", value="a")])
    assert budget["applied"] == 1
    assert client.peek_endpoint(seat_id).startswith("vnc://")

    # repo injection + export (long ops through job handles)
    assert (
        client.prepare_repo(
            seat_id, RepoSpec(url="https://example.com/x.git", branch="main")
        ).result(timeout=5)
        is None
    )
    prepared = daemon_env.fake.prepared_repos
    assert any(spec.url == "https://example.com/x.git" for spec in prepared.values())

    outcome = client.export_seat(
        seat_id, repo="demo", branch="task", ref="origin:refs/heads/task"
    ).result(timeout=5)
    assert outcome["ok"] is True
    specs = [args[1] for name, args, _ in daemon_env.fake.calls if name == "fetch_bundle"]
    assert specs[-1].branch == "task"
    assert specs[-1].ref == "origin:refs/heads/task"

    # admission controls
    assert client.set_admission_override("allow")["override"] == "allow"
    assert client.clear_prewarm_backoff()["seat_type"] is None

    # reconcile is a job too
    report = client.reconcile().result(timeout=5)
    assert report["orphans_destroyed"] == 0

    # release destroys
    release = client.release_seat(seat_id, export=False).result(timeout=5)
    assert release["destroyed"] is True
    assert client.list_seats() == []


def test_cancel_request_through_daemon(daemon_env):
    client = daemon_env.client
    client.set_admission_override("deny")
    request = client.request_seat("queued-agent", "terminal")
    assert request.status()["status"] == "waiting"
    cancelled = client.cancel_request(request.request_id).result(timeout=5)
    assert cancelled["status"] == "cancelled"
    assert request.wait_ready(timeout=5)["status"] == "cancelled"


# --------------------------------------------------------------------------
# concurrency / non-blocking long ops
# --------------------------------------------------------------------------
def test_multiple_concurrent_clients(daemon_env):
    errors: list[BaseException] = []

    def worker() -> None:
        client = DaemonClient(socket_path=daemon_env.socket_path)
        try:
            for _ in range(15):
                status = client.pool_status()
                assert "per_type" in status
        except BaseException as exc:  # noqa: BLE001 - propagated to the assert
            errors.append(exc)
        finally:
            client.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert errors == []


def test_long_op_does_not_block_other_clients(daemon_env):
    daemon_env.fake.export_delay_s = 1.0
    client = daemon_env.client
    request, seat_id = _ready_seat(client)

    job = client.release_seat(seat_id, repo="demo", branch="task")
    # A concurrent client must be served while the export is in flight.
    other = DaemonClient(socket_path=daemon_env.socket_path)
    try:
        started = time.monotonic()
        assert other.ping() is True
        assert time.monotonic() - started < 0.5
        assert job.poll()["state"] == "pending"
        result = job.result(timeout=10)
        assert result["destroyed"] is True
    finally:
        other.close()
    assert request.request_id  # request stays addressable


# --------------------------------------------------------------------------
# restart / state survival
# --------------------------------------------------------------------------
def test_restart_reattaches_seat_and_survives_state(daemon_env, tmp_path):
    client = daemon_env.client
    _, seat_id = _ready_seat(client)
    client.close()
    daemon_env.server.shutdown()
    daemon_env.thread.join(timeout=5)
    assert not daemon_env.socket_path.exists()

    manager = Manager(
        daemon_env.config,
        db_path=daemon_env.db,
        provisioner=daemon_env.fake,
        free_ram_mb=lambda: 10**9,
    )
    server = DaemonServer(manager, socket_path=daemon_env.socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_socket(daemon_env.socket_path)
    revived = DaemonClient(socket_path=daemon_env.socket_path)
    try:
        seats = revived.list_seats()
        assert [s["id"] for s in seats] == [seat_id]
        assert seats[0]["state"] == "ready"
        assert seats[0]["vm_name"] == "fake://terminal-1"
        assert daemon_env.fake.destroyed == []
        assert manager.last_reconcile_report.orphans_destroyed == 0
        assert revived.ping() is True
    finally:
        revived.release_seat(seat_id, export=False).result(timeout=5)
        revived.close()
        server.shutdown()
        thread.join(timeout=5)


# --------------------------------------------------------------------------
# malformed / structured errors
# --------------------------------------------------------------------------
def _raw_request(socket_path, payload: bytes) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(socket_path))
    try:
        sock.sendall(payload + b"\n")
        line = sock.makefile("rb").readline()
        return json.loads(line)
    finally:
        sock.close()


def test_malformed_and_unknown_requests(daemon_env):
    path = daemon_env.socket_path
    assert _raw_request(path, b"this is not json")["error"]["code"] == "bad_request"
    assert _raw_request(path, b'{"id": 1}')["error"]["code"] == "bad_request"
    unknown = _raw_request(path, b'{"id": 2, "method": "nope"}')
    assert unknown["error"]["code"] == "unknown_method"
    invalid = _raw_request(
        path, b'{"id": 3, "method": "seat_status", "params": {"request_id": "x"}}'
    )
    assert invalid["error"]["code"] == "invalid"
    missing = _raw_request(path, b'{"id": 4, "method": "exec_poll", "params": {"seat_id": 1}}')
    assert missing["error"]["code"] == "invalid"
    # the server survives every malformed request
    assert daemon_env.client.ping() is True


def test_client_raises_typed_errors(daemon_env):
    with pytest.raises(DaemonRequestError) as excinfo:
        daemon_env.client.seat_status(999_999)
    assert excinfo.value.code == "not_found"

    with pytest.raises(DaemonRequestError) as excinfo:
        daemon_env.client.job_poll("job-does-not-exist")
    assert excinfo.value.code == "not_found"


def test_job_error_surfaces_as_structured_error(daemon_env):
    job = daemon_env.client.reset_seat(999_999)
    with pytest.raises(DaemonRequestError) as excinfo:
        job.result(timeout=5)
    assert excinfo.value.code == "not_found"


# --------------------------------------------------------------------------
# single instance / lifecycle / permissions
# --------------------------------------------------------------------------
def test_second_daemon_is_refused(daemon_env, tmp_path):
    manager = Manager(
        daemon_env.config,
        db_path=tmp_path / "other.db",
        provisioner=FakeProvisioner(),
        free_ram_mb=lambda: 10**9,
    )
    second = DaemonServer(manager, socket_path=daemon_env.socket_path)
    with pytest.raises(DaemonAlreadyRunning):
        second.bind()
    # the incumbent keeps serving
    assert daemon_env.client.ping() is True


def test_socket_and_directory_permissions(daemon_env):
    socket_mode = stat.S_IMODE(os.stat(daemon_env.socket_path).st_mode)
    assert socket_mode == 0o600
    dir_mode = stat.S_IMODE(os.stat(daemon_env.socket_path.parent).st_mode)
    assert dir_mode == 0o700


def test_graceful_shutdown_leaves_state_recoverable(daemon_env):
    client = daemon_env.client
    _, seat_id = _ready_seat(client)
    client.close()
    daemon_env.server.shutdown()
    daemon_env.thread.join(timeout=5)

    assert not daemon_env.socket_path.exists()
    # no seat was destroyed: running seats are reattachable on next start
    assert daemon_env.fake.destroyed == []
    seats = daemon_env.manager.list_seats(include_history=True)
    assert any(seat.id == seat_id and seat.state == "ready" for seat in seats)


def test_client_not_running_reports_clear_error(tmp_path):
    client = DaemonClient(socket_path=tmp_path / "missing.sock", connect_retries=0)
    with pytest.raises(DaemonNotRunning):
        client.ping()


# --------------------------------------------------------------------------
# factory / logging
# --------------------------------------------------------------------------
def test_build_provisioner_fake_and_unknown():
    assert isinstance(build_provisioner("fake", Config.default()), FakeProvisioner)
    with pytest.raises(ValueError, match="unknown provisioner"):
        build_provisioner("toaster", Config.default())


def test_configure_logging_writes_to_file(tmp_path):
    log_file = tmp_path / "daemon.log"
    configure_logging(log_file, "DEBUG")
    import logging

    logging.getLogger("omavroom.daemon.test").info("hello-daemon-log")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hello-daemon-log" in log_file.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# frozen protocol items (FIX 3)
# --------------------------------------------------------------------------
def test_ping_reports_protocol_version(daemon_env):
    info = daemon_env.client.ping_info()
    assert info == {"pong": True, "protocol": PROTOCOL_VERSION}
    assert daemon_env.client.protocol_version() == PROTOCOL_VERSION


def test_exec_start_poll_finish_fields(daemon_env):
    # Frozen v1 shapes: with no ``command`` an exec is bookkeeping-only and
    # the client drives output/finish itself (the 4B2 behaviour).
    client = daemon_env.client
    _, seat_id = _ready_seat(client)
    started = client.exec_start(seat_id, "e1", label="build", timeout_s=120)
    assert started["command"] is None
    assert started["timeout_s"] == 120
    assert started["stdout"] == "" and started["stderr"] == ""

    client.exec_output(seat_id, "e1", stdout="partial\n")
    polled = client.exec_poll(seat_id, "e1")
    assert polled["stdout"] == "partial\n"
    assert polled["stderr"] == ""
    assert polled["exit_code"] is None
    assert polled["truncated"] is False

    finished = client.exec_finish(seat_id, "e1", exit_code=0, stdout="done\n")
    assert finished["state"] == "finished"
    assert finished["exit_code"] == 0
    assert finished["stdout"] == "partial\ndone\n"


def test_exec_command_runs_and_streams_through_daemon(daemon_env):
    # Phase 5: a command exec runs on a dedicated worker and its output is
    # polled from the bounded ring buffer.
    fake = daemon_env.fake
    fake.run_chunks = [
        (0.0, "stdout", "building...\n"),
        (0.02, "stderr", "warning: x\n"),
        (0.02, "stdout", "done\n"),
    ]
    fake.run_exit_code = 0
    client = daemon_env.client
    _, seat_id = _ready_seat(client)

    started = client.exec_start(seat_id, "cmd-1", label="build", command="make test", timeout_s=30)
    assert started["state"] == "running"
    assert started["command"] == "make test"

    deadline = time.monotonic() + 5
    view = client.exec_poll(seat_id, "cmd-1")
    while view["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        view = client.exec_poll(seat_id, "cmd-1")
    assert view["state"] == "finished"
    assert view["exit_code"] == 0
    assert "building..." in view["stdout"] and "done" in view["stdout"]
    assert "warning: x" in view["stderr"]
    # The worker records the terminal exec state just before it flips the
    # seat busy -> ready, so wait for the seat transition rather than assume.
    deadline = time.monotonic() + 5
    while client.list_seats()[0]["state"] != "ready" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.list_seats()[0]["state"] == "ready"


def test_exec_rejected_on_released_seat_over_wire(daemon_env):
    client = daemon_env.client
    _, seat_id = _ready_seat(client)
    client.release_seat(seat_id, export=False).result(timeout=5)
    with pytest.raises(DaemonRequestError) as excinfo:
        client.exec_start(seat_id, "e1", command="echo hi")
    assert excinfo.value.code == "exec_not_allowed"


def test_screenshot_default_and_hard_cap(daemon_env):
    client = daemon_env.client
    _, seat_id = _ready_seat(client)
    default = client.call("screenshot", seat_id=seat_id)
    assert default["max_width"] == DEFAULT_SCREENSHOT_MAX_WIDTH
    assert default["hard_cap"] == MAX_SCREENSHOT_MAX_WIDTH
    huge = client.call("screenshot", seat_id=seat_id, max_width=999_999)
    assert huge["max_width"] == MAX_SCREENSHOT_MAX_WIDTH
    import base64

    png = base64.b64decode(huge["png_base64"])
    assert png.startswith(b"PNG:")


def test_retry_release_and_force_discard_over_wire(daemon_env):
    client = daemon_env.client
    daemon_env.fake.fail_exports = True
    _, seat_id = _ready_seat(client)
    released = client.release_seat(seat_id, repo="demo", branch="task").result(timeout=5)
    assert released["held"] is True
    assert seat_id in client.pool_status()["needs_attention"]

    daemon_env.fake.fail_exports = False
    retried = client.retry_release(seat_id).result(timeout=5)
    assert retried["destroyed"] is True
    assert client.pool_status()["needs_attention"] == []

    # force_discard is also directly usable
    _, seat2 = _ready_seat(client, "agent-2")
    forced = client.force_discard(seat2, reason="test").result(timeout=5)
    assert forced["state"] == "off"


def test_daemon_resumes_interrupted_release_on_start(tmp_path):
    cfg = Config.default()
    cfg.host.headroom_floor_mb = 512
    cfg.seats["terminal"].max_seats = 1
    fake = FakeProvisioner()
    vm_ref = fake.inject_vm("terminal-1", seat_type="terminal", state="running")
    db = tmp_path / "state.db"
    store = st.StateStore(db)
    store.init()
    now = st.fmt_time(st.utcnow())
    with store.transaction() as conn:
        seat_id = st.insert_seat(
            conn,
            name="terminal-1",
            seat_type="terminal",
            image="golden-term",
            state="releasing",
            vm_name=vm_ref,
            now=now,
        )
        st.update_seat(
            conn,
            seat_id,
            pending_action="release",
            pending_export=1,
            pending_repo="demo",
            pending_branch="task",
            pending_request_status="done",
            now=now,
        )

    manager = Manager(cfg, db_path=db, provisioner=fake, free_ram_mb=lambda: 10**9)
    socket_path = tmp_path / "run" / "daemon.sock"
    server = DaemonServer(manager, socket_path=socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_socket(socket_path)
    client = DaemonClient(socket_path=socket_path)
    try:
        seats = client.list_seats(include_history=True)
        assert [s["state"] for s in seats] == ["off"]
        assert fake.destroyed == [vm_ref]
        assert manager.last_reconcile_report.seats_resumed >= 1
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=5)


# --------------------------------------------------------------------------
# client hardening (FIX 3)
# --------------------------------------------------------------------------
class _RogueStream:
    """A fake socket stream that answers with a caller-controlled response."""

    def __init__(self, factory, *, timeout: bool = False) -> None:
        self.factory = factory
        self.timeout = timeout
        self.written = b""

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def readline(self) -> bytes:
        if self.timeout:
            raise TimeoutError("simulated read timeout")
        request = json.loads(self.written.decode("utf-8"))
        return (json.dumps(self.factory(request)) + "\n").encode("utf-8")


def test_client_verifies_echoed_response_id(tmp_path):
    client = DaemonClient(socket_path=tmp_path / "nope.sock", connect_retries=0)
    client._stream = _RogueStream(lambda req: {"id": req["id"] + 1, "ok": True, "result": {}})
    with pytest.raises(DaemonRequestError) as excinfo:
        client._call_locked("ping", {})
    assert excinfo.value.code == "bad_response"


def test_client_maps_already_running_error(tmp_path):
    def factory(req):
        return {
            "id": req["id"],
            "ok": False,
            "error": {"code": "already_running", "message": "busy"},
        }

    client = DaemonClient(socket_path=tmp_path / "nope.sock", connect_retries=0)
    client._stream = _RogueStream(factory)
    with pytest.raises(ClientAlreadyRunning) as excinfo:
        client._call_locked("ping", {})
    assert excinfo.value.code == "already_running"


def test_client_converts_read_timeout_to_typed_error(tmp_path):
    client = DaemonClient(socket_path=tmp_path / "nope.sock", connect_retries=0)
    client._stream = _RogueStream(lambda req: {}, timeout=True)
    with pytest.raises(DaemonTimeout):
        client._call_locked("ping", {})


# --------------------------------------------------------------------------
# connection guard rails (FIX 4)
# --------------------------------------------------------------------------
def test_request_line_too_long_is_rejected(daemon_env, monkeypatch):
    import omavroom.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "MAX_LINE_BYTES", 64)
    response = _raw_request(daemon_env.socket_path, b"x" * 200)
    assert response["error"]["code"] == "bad_request"
    assert "too long" in response["error"]["message"]
    assert daemon_env.client.ping() is True


def test_request_line_exactly_at_cap_is_accepted(daemon_env, monkeypatch):
    import omavroom.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "MAX_LINE_BYTES", 64)
    payload = json.dumps({"id": 7, "method": "ping"}).encode("ascii")
    padded = payload + b" " * (64 - 1 - len(payload))
    assert len(padded) + 1 == 64  # including the trailing newline
    response = _raw_request(daemon_env.socket_path, padded)
    assert response["ok"] is True
    assert response["result"]["pong"] is True


def test_connection_cap_refuses_excess(daemon_env, monkeypatch):
    import omavroom.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "MAX_CONNECTIONS", 2)
    path = daemon_env.socket_path
    held = []
    try:
        for _ in range(2):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(str(path))
            held.append(sock)
        time.sleep(0.1)  # let the accept loop register both
        third = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        third.settimeout(5)
        third.connect(str(path))
        assert third.recv(1) == b""  # accepted then immediately closed
        third.close()
    finally:
        for sock in held:
            sock.close()
    time.sleep(0.1)
    assert daemon_env.client.ping() is True
