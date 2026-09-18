"""Phase 6 CLI tests against an in-process fake-provisioner daemon.

No VMs, no host sudo: every command is exercised end-to-end through the real
protocol-v1 client against a :class:`FakeProvisioner` daemon.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from omavroom import cli
from omavroom.client import DaemonClient, DaemonNotRunning
from omavroom.config import Config


def _ready(pool, agent, seat_type="terminal", **kwargs):
    """Request a seat through the real client and return its seat view."""
    client = DaemonClient(pool.socket_path)
    try:
        view = client.request_seat(agent, seat_type, **kwargs).wait_ready(timeout=5)
        assert view.get("seat") is not None, view
        return view["seat"]
    finally:
        client.close()


def _cli(pool, *args: str) -> int:
    return cli.main(["--socket", str(pool.socket_path), *args])


# -- status / seats / queue --------------------------------------------------
def test_status_table(fake_daemon, capsys):
    with fake_daemon() as pool:
        _ready(pool, "alice", "desktop", project="web")
        assert _cli(pool, "status") == 0
        out = capsys.readouterr().out
        assert "SEATS" in out
        assert "alice" in out and "web" in out
        assert "desktop 1/1" in out


def test_status_json_shape(fake_daemon, capsys):
    with fake_daemon() as pool:
        assert _cli(pool, "status", "--json") == 0
        status = json.loads(capsys.readouterr().out)
        assert set(status) >= {"free_ram_mb", "headroom_floor_mb", "seats", "queue", "per_type"}
        assert set(status["per_type"]) == {"desktop", "terminal"}


def test_seats_human_and_json(fake_daemon, capsys):
    with fake_daemon() as pool:
        _ready(pool, "alice", "terminal", project="api")
        assert _cli(pool, "seats") == 0
        assert "alice" in capsys.readouterr().out
        assert _cli(pool, "seats", "--json") == 0
        seats = json.loads(capsys.readouterr().out)
        assert len(seats) == 1 and seats[0]["agent_label"] == "alice"


def test_queue_shows_position_and_next(fake_daemon, capsys):
    cfg = Config.default()
    cfg.seats["desktop"].max_seats = 0
    cfg.seats["terminal"].max_seats = 1
    with fake_daemon(config=cfg) as pool:
        _ready(pool, "first", "terminal")
        client = DaemonClient(pool.socket_path)
        try:
            client.request_seat("second", "terminal", project="later")
        finally:
            client.close()
        assert _cli(pool, "queue") == 0
        out = capsys.readouterr().out
        assert "second" in out and "NEXT" in out
        assert _cli(pool, "queue", "--json") == 0
        waiting = json.loads(capsys.readouterr().out)
        assert [r["agent_label"] for r in waiting] == ["second"]


# -- request lifecycle -------------------------------------------------------
def test_request_wait_json(fake_daemon, capsys):
    with fake_daemon() as pool:
        rc = _cli(
            pool,
            "request",
            "terminal",
            "--agent",
            "zoe",
            "--project",
            "api",
            "--wait",
            "--json",
        )
        assert rc == 0
        view = json.loads(capsys.readouterr().out)
        assert view["status"] == "claimed"
        assert view["seat"]["state"] == "ready"
        assert view["project"] == "api"


def test_request_without_wait_prints_id(fake_daemon, capsys):
    with fake_daemon() as pool:
        assert _cli(pool, "request", "terminal", "--agent", "zoe") == 0
        assert capsys.readouterr().out.strip().isdigit()


# -- desktop ops -------------------------------------------------------------
def test_screenshot_writes_file(fake_daemon, tmp_path, capsys):
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "desktop")
        out_path = tmp_path / "shot.png"
        rc = _cli(pool, "screenshot", seat["name"], "-o", str(out_path), "--json")
        assert rc == 0
        meta = json.loads(capsys.readouterr().out)
        assert meta["path"] == str(out_path)
        assert out_path.exists()
        assert out_path.stat().st_size == meta["bytes"] > 0


def test_screenshot_default_path_is_outside_cwd(fake_daemon, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "desktop")
        assert _cli(pool, "screenshot", seat["name"], "--json") == 0
        meta = json.loads(capsys.readouterr().out)
        path = Path(meta["path"])
        assert path.parent == home / ".local" / "share" / "omavroom" / "screenshots"
        assert path.parent.is_dir()
        assert path.exists() and path.stat().st_size == meta["bytes"] > 0
        assert not (Path.cwd() / path.name).exists()


def test_screenshot_bad_output_path_is_clean(fake_daemon, tmp_path, capsys):
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "desktop")
        bad = tmp_path / "does-not-exist" / "shot.png"
        assert _cli(pool, "screenshot", seat["name"], "-o", str(bad)) == 1
        assert "cannot write screenshot" in capsys.readouterr().err
        assert not bad.exists()


def test_screenshot_directory_as_output_is_clean(fake_daemon, tmp_path, capsys):
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "desktop")
        assert _cli(pool, "screenshot", seat["name"], "-o", str(tmp_path)) == 1
        assert "cannot write screenshot" in capsys.readouterr().err


def test_screenshot_unknown_seat_is_clean(fake_daemon, capsys):
    with fake_daemon() as pool:
        assert _cli(pool, "screenshot", "ghost") == 1
        assert "no seat named" in capsys.readouterr().err


def test_peek_prints_endpoint_and_json(fake_daemon, capsys):
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "desktop")
        assert _cli(pool, "peek", seat["name"]) == 0
        assert capsys.readouterr().out.startswith("vnc://")
        assert _cli(pool, "peek", seat["name"], "--json") == 0
        data = json.loads(capsys.readouterr().out)
        assert data["endpoint"].startswith("vnc://")


# -- teardown / recovery -----------------------------------------------------
def test_release_no_export_destroys(fake_daemon, capsys):
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "terminal")
        assert _cli(pool, "release", seat["name"], "--no-export", "--json") == 0
        result = json.loads(capsys.readouterr().out)
        assert result["destroyed"] is True and result["held"] is False


def test_reset_keeps_seat(fake_daemon, capsys):
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "terminal")
        assert _cli(pool, "reset", seat["name"]) == 0
        assert "state=ready" in capsys.readouterr().out


def test_force_discard_unblocks(fake_daemon, capsys):
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "terminal")
        assert _cli(pool, "force-discard", str(seat["id"]), "--reason", "test") == 0
        assert "state=off" in capsys.readouterr().out


def test_retry_release_without_intent_errors(fake_daemon, capsys):
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "terminal")
        assert _cli(pool, "retry-release", seat["name"]) == 1
        assert "no persisted release intent" in capsys.readouterr().err


def test_destroy_destroys_without_export(fake_daemon, capsys):
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "terminal")
        assert _cli(pool, "destroy", seat["name"], "--reason", "runaway agent") == 0
        assert capsys.readouterr().out.strip() == f"destroy seat {seat['id']}: state=off"
        # The live pool no longer lists it (destroyed, not held).
        client = DaemonClient(pool.socket_path)
        try:
            assert all(s["id"] != seat["id"] for s in client.list_seats())
        finally:
            client.close()


def test_destroy_json(fake_daemon, capsys):
    with fake_daemon() as pool:
        seat = _ready(pool, "alice", "terminal")
        assert _cli(pool, "destroy", str(seat["id"]), "--json") == 0
        result = json.loads(capsys.readouterr().out)
        assert result["state"] == "off" and result["vm_name"] is None


# -- admission / images ------------------------------------------------------
def test_admission_override_round_trip(fake_daemon, capsys):
    with fake_daemon() as pool:
        assert _cli(pool, "admission", "--override", "deny") == 0
        assert capsys.readouterr().out.strip() == "admission override set to deny"
        assert _cli(pool, "status", "--json") == 0
        assert json.loads(capsys.readouterr().out)["admission_override"] == "deny"
        assert _cli(pool, "admission") == 0
        assert "admission override: deny" in capsys.readouterr().out


def test_image_list_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OMAVROOM_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["image", "list"]) == 0
    assert "golden-desktop" in capsys.readouterr().out
    assert cli.main(["image", "list", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert any(entry["name"] == "golden-term" for entry in entries)


def test_events_table(fake_daemon, capsys):
    with fake_daemon() as pool:
        _ready(pool, "alice", "terminal")
        assert _cli(pool, "events", "--limit", "5") == 0
        assert "seat_" in capsys.readouterr().out


# -- settings / config show --------------------------------------------------
def test_settings_offline_human_and_json(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OMAVROOM_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["settings"]) == 0
    out = capsys.readouterr().out
    assert "headroom_floor_mb=2048" in out
    assert "leases:" in out and "exec:" in out and "export:" in out
    assert "SEATS" in out and "RESOURCES" in out

    assert cli.main(["settings", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["seats"]["desktop"]["max_seats"] == 1
    assert data["seats"]["terminal"]["cost_units"] == 1
    assert data["host"]["headroom_floor_mb"] == 2048
    assert data["leases"]["heartbeat_timeout_s"] == 300
    assert data["exec"]["max_concurrent_per_seat"] == 4
    assert data["export"]["max_files_changed"] == 200
    assert data["export"]["protected_paths"] == [".git/", ".github/"]


def test_config_show_alias(tmp_path, monkeypatch, capsys):
    config_file = tmp_path / "omavroom.toml"
    config_file.write_text(
        "[seats.desktop]\nmax_seats = 3\n[host]\nheadroom_floor_mb = 777\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMAVROOM_CONFIG", str(config_file))
    assert cli.main(["config", "show", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["seats"]["desktop"]["max_seats"] == 3
    assert data["host"]["headroom_floor_mb"] == 777


def test_settings_bad_config_is_clean(tmp_path, monkeypatch, capsys):
    config_file = tmp_path / "bad.toml"
    config_file.write_text("[nonsense]\nkey = 1\n", encoding="utf-8")
    monkeypatch.setenv("OMAVROOM_CONFIG", str(config_file))
    assert cli.main(["settings"]) == 1
    assert "unknown config sections" in capsys.readouterr().err


# -- watch / down / usage ----------------------------------------------------
def test_status_watch_iterations(fake_daemon, capsys):
    with fake_daemon() as pool:
        rc = cli.main(
            [
                "--socket",
                str(pool.socket_path),
                "status",
                "--watch",
                "--iterations",
                "2",
                "--interval",
                "0",
            ]
        )
        assert rc == 0
        assert capsys.readouterr().out.count("SEATS") == 2


def test_watch_prints_all_and_reports_down_exit(tmp_path, capsys):
    rc = cli.main(
        [
            "--socket",
            str(tmp_path / "missing.sock"),
            "status",
            "--watch",
            "--iterations",
            "2",
            "--interval",
            "0",
        ]
    )
    # All iterations are printed; the last poll was daemon-down, so exit 1.
    assert rc == 1
    out = capsys.readouterr().out
    assert out.count("daemon not running") == 2


def _pool_snapshot():
    return {
        "free_ram_mb": 8192,
        "headroom_floor_mb": 2048,
        "admission_override": "auto",
        "seats": [],
        "queue": [],
        "per_type": {},
        "needs_attention": [],
    }


class _FlakyClient:
    """Stub client returning a scripted sequence of snapshots/errors."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.closed = False

    def pool_status(self):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


def test_watch_exit_reflects_last_poll_only(capsys):
    args = argparse.Namespace(iterations=2, interval=0.0, json=False)
    # down then up -> successful final poll exits 0
    recovered = _FlakyClient([DaemonNotRunning("down"), _pool_snapshot()])
    assert cli._watch(recovered, args) == 0
    assert recovered.calls == 2 and recovered.closed
    # up then down -> failing final poll exits 1, but both were printed
    lost = _FlakyClient([_pool_snapshot(), DaemonNotRunning("down")])
    assert cli._watch(lost, args) == 1
    out = capsys.readouterr().out
    assert "daemon not running" in out and "pool: free" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["status"],
        ["seats"],
        ["queue"],
        ["request", "terminal", "--agent", "zoe"],
        ["screenshot", "1"],
        ["peek", "1"],
        ["release", "1"],
        ["reset", "1"],
        ["retry-release", "1"],
        ["force-discard", "1"],
        ["destroy", "1"],
        ["events"],
        ["admission"],
    ],
)
def test_commands_error_when_daemon_down(tmp_path, capsys, argv):
    rc = cli.main(["--socket", str(tmp_path / "missing.sock"), *argv])
    assert rc == 1
    assert "not running" in capsys.readouterr().err.lower()


def test_stub_command_still_exits_2(capsys):
    assert cli.main(["exec"]) == 2
    assert "not yet implemented" in capsys.readouterr().err


def test_image_without_action_exits_2(capsys):
    assert cli.main(["image"]) == 2
    assert "choose an action" in capsys.readouterr().err


def test_config_without_action_exits_2(capsys):
    assert cli.main(["config"]) == 2
    assert "choose an action" in capsys.readouterr().err


def test_destroy_has_reason_option():
    parser = cli.build_parser()
    args = parser.parse_args(["destroy", "1", "--reason", "runaway"])
    assert args.command == "destroy"
    assert args.reason == "runaway"
    assert parser.parse_args(["destroy", "1"]).reason == "destroy"
    assert parser.parse_args(["force-discard", "1"]).reason == "force_discard"
