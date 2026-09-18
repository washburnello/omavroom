"""CLI wiring for the daemon (Phase 4B2): parser, `status`, `daemon` seam."""

from __future__ import annotations

import threading
import time

from omavroom import cli
from omavroom.config import Config
from omavroom.manager import Manager
from omavroom.manager.provisioner import FakeProvisioner


def _start_daemon(tmp_path):
    manager = Manager(
        Config.default(),
        db_path=tmp_path / "state.db",
        provisioner=FakeProvisioner(),
        free_ram_mb=lambda: 10**9,
    )
    from omavroom.daemon import DaemonServer

    server = DaemonServer(manager)  # default path: $XDG_RUNTIME_DIR/omavroom/daemon.sock
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.socket_path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("daemon did not start")
        time.sleep(0.01)
    return server, thread


def test_parser_daemon_provisioner_choice():
    parser = cli.build_parser()
    args = parser.parse_args(["daemon", "--provisioner", "fake"])
    assert args.command == "daemon"
    assert args.provisioner == "fake"


def test_cli_status_against_running_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    server, thread = _start_daemon(tmp_path)
    try:
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "per_type" in out
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_cli_status_without_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("OMAVROOM_PROVISIONER", raising=False)
    assert cli.main(["status"]) == 1
    assert "not running" in capsys.readouterr().err.lower()


def test_cli_stub_commands_keep_nonzero_exit(capsys):
    assert cli.main(["request"]) == 2
    assert "not yet implemented" in capsys.readouterr().err


def test_run_daemon_foreground_wiring(tmp_path, monkeypatch):
    from omavroom import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod.DaemonServer, "serve_forever", lambda self: None)
    rc = daemon_mod.run_daemon(
        provisioner="fake",
        config=Config.default(),
        db_path=tmp_path / "state.db",
        socket_path=tmp_path / "daemon.sock",
        log_path=tmp_path / "daemon.log",
    )
    assert rc == 0
    assert not (tmp_path / "daemon.sock").exists()
    assert (tmp_path / "daemon.log").exists()


def test_parser_accepts_real_alias():
    parser = cli.build_parser()
    args = parser.parse_args(["daemon", "--provisioner", "real"])
    assert args.provisioner == "real"


def test_run_daemon_invalid_provisioner_is_clean(tmp_path, capsys):
    from omavroom import daemon as daemon_mod

    rc = daemon_mod.run_daemon(
        provisioner="toaster",
        config=Config.default(),
        db_path=tmp_path / "state.db",
        socket_path=tmp_path / "daemon.sock",
        log_path=tmp_path / "daemon.log",
    )
    assert rc == 2
    assert "unknown provisioner" in capsys.readouterr().err
    assert not (tmp_path / "daemon.sock").exists()


def test_cli_operator_actions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    server, thread = _start_daemon(tmp_path)
    from omavroom.client import DaemonClient

    client = DaemonClient()
    try:
        request = client.request_seat("cli-agent", "terminal")
        view = request.wait_ready(timeout=5)
        seat_id = view["seat"]["id"]
        assert cli.main(["force-discard", str(seat_id)]) == 0
        assert "off" in capsys.readouterr().out
        assert not any(s["id"] == seat_id and s["state"] != "off" for s in client.list_seats())
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=5)
