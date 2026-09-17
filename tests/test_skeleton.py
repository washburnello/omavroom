"""Skeleton tests for Phase 0: config defaults, CLI entry, sqlite schema."""

import subprocess
import sys

import pytest

from omavroom import state
from omavroom.cli import STUB_COMMANDS, main
from omavroom.config import Config


def test_config_defaults_match_plan_starting_point() -> None:
    cfg = Config.default()
    assert cfg.capacity.total_units == 4
    assert cfg.seats["desktop"].cost_units == 4
    assert cfg.seats["terminal"].cost_units == 1
    for seat_type in ("desktop", "terminal"):
        seat = cfg.seats[seat_type]
        assert seat.min_seats <= seat.max_seats
    assert cfg.host.headroom_floor_mb > 0
    assert cfg.leases.lease_timeout_s >= cfg.leases.heartbeat_timeout_s
    assert cfg.leases.heartbeat_timeout_s >= cfg.leases.heartbeat_interval_s
    assert cfg.exec.max_output_bytes > 0
    assert cfg.exec.max_runtime_s > 0


def test_config_toml_override(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(
        "[capacity]\ntotal_units = 8\n"
        "[seats.desktop]\nmax_seats = 2\n"
        "[host]\nheadroom_floor_mb = 1024\n",
        encoding="utf-8",
    )
    cfg = Config.from_toml(cfg_file)
    assert cfg.capacity.total_units == 8
    assert cfg.seats["desktop"].max_seats == 2
    # Untouched keys keep defaults.
    assert cfg.seats["desktop"].cost_units == 4
    assert cfg.seats["terminal"].cost_units == 1
    assert cfg.host.headroom_floor_mb == 1024


def test_config_rejects_bad_values(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text("[seats.desktop]\nmin_seats = 3\nmax_seats = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        Config.from_toml(cfg_file)


@pytest.mark.parametrize("section", ["capacity", "host", "leases", "exec"])
def test_config_rejects_unknown_keys(tmp_path, section) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(f"[{section}]\nbogus_key = 99\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"unknown keys in \[{section}\].*bogus_key"):
        Config.from_toml(cfg_file)


@pytest.mark.parametrize("seat_type", ["desktop", "terminal"])
def test_config_rejects_unknown_seat_keys(tmp_path, seat_type) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(f"[seats.{seat_type}]\nbogus_key = 99\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"unknown keys in \[seats\.{seat_type}\].*bogus_key"):
        Config.from_toml(cfg_file)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('[capacity]\ntotal_units = "four"\n', r"capacity\.total_units"),
        ('[seats]\ndesktop = "foo"\n', r"\[seats\.desktop\] must be a table"),
        ("capacity = 5\n", r"\[capacity\] must be a table"),
        ('seats = "foo"\n', r"\[seats\] must be a table"),
        ("[capacity]\ntotal_units = 4.5\n", r"capacity\.total_units"),
        ("[capacity]\ntotal_units = true\n", r"capacity\.total_units"),
    ],
)
def test_config_rejects_shape_violations(tmp_path, body, match) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        Config.from_toml(cfg_file)


def test_config_coerces_integral_floats(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text("[capacity]\ntotal_units = 4.0\n", encoding="utf-8")
    assert Config.from_toml(cfg_file).capacity.total_units == 4


def test_cli_help_lists_stub_subcommands() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "omavroom", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    for cmd in STUB_COMMANDS:
        assert cmd in proc.stdout


@pytest.mark.parametrize("cmd", STUB_COMMANDS)
def test_cli_stub_subcommand_runs(cmd, capsys) -> None:
    assert main([cmd]) == 2
    captured = capsys.readouterr()
    assert "not yet implemented" in captured.err
    assert captured.out == ""


def test_sqlite_init_creates_tables(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    conn = state.connect(db_path)
    try:
        state.init_schema(conn)
        assert {"seats", "leases", "queue"} <= set(state.list_tables(conn))
    finally:
        conn.close()


def test_sqlite_connection_uses_wal(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    conn = state.connect(db_path)
    try:
        (mode,) = conn.execute("PRAGMA journal_mode;").fetchone()
        assert mode == "wal"
    finally:
        conn.close()
