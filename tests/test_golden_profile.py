"""Golden-image source profile: config parse/validate, persistence, daemon, CLI.

No VMs and no real user config: every test pins ``XDG_CONFIG_HOME`` (and
clears ``OMAVROOM_CONFIG``) so ``~/.config/omavroom/config.toml`` is never read
or written.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from omavroom import cli
from omavroom.client import DaemonClient, DaemonRequestError
from omavroom.config import (
    GOLDEN_PROFILES,
    Config,
    GoldenConfig,
    default_config_path,
    set_config_value,
)


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    """Point the per-user config path at a fresh tmp XDG dir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OMAVROOM_CONFIG", raising=False)
    return default_config_path()


# -- parse / validate --------------------------------------------------------
def test_golden_default_profile() -> None:
    cfg = Config.default()
    assert cfg.golden.profile == "stock"
    assert cfg.golden_profile == "stock"
    assert GOLDEN_PROFILES == ("stock", "mirror")


def test_golden_toml_override(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text('[golden]\nprofile = "mirror"\n', encoding="utf-8")
    assert Config.from_toml(cfg_file).golden_profile == "mirror"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('[golden]\nprofile = "gold"\n', r"golden\.profile must be one of"),
        ("[golden]\nprofile = 5\n", r"golden\.profile must be a non-empty string"),
        ('[golden]\nprofile = ""\n', r"golden\.profile must be a non-empty string"),
        ("[golden]\nbogus = 1\n", r"unknown keys in \[golden\].*bogus"),
        ('[gold]\nprofile = "stock"\n', r"unknown config sections"),
    ],
)
def test_bad_golden_values_rejected(tmp_path, body, match) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        Config.from_toml(cfg_file)


def test_golden_config_validates_directly() -> None:
    with pytest.raises(ValueError):
        GoldenConfig(profile="nope")


def test_validate_and_set_value() -> None:
    cfg = Config.default()
    assert Config.validate_value("golden", "profile", "mirror") == "mirror"
    assert cfg.set_value("golden", "profile", "mirror") == "mirror"
    assert cfg.golden_profile == "mirror"
    with pytest.raises(ValueError, match="unknown config section"):
        Config.validate_value("nope", "profile", "stock")
    with pytest.raises(ValueError, match=r"unknown key in \[golden\]"):
        Config.validate_value("golden", "nope", "stock")
    # ``validate_value`` coerces the declared type; the profile enum lives in
    # the dataclass and is enforced by ``set_value`` / ``set_config_value``.
    assert Config.validate_value("golden", "profile", "nope") == "nope"
    with pytest.raises(ValueError, match=r"golden\.profile must be one of"):
        Config.default().set_value("golden", "profile", "nope")


# -- load precedence ---------------------------------------------------------
def test_load_defaults_when_no_file(user_config) -> None:
    assert not user_config.exists()
    assert Config.load().golden_profile == "stock"


def test_load_reads_xdg_user_config(user_config) -> None:
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text('[golden]\nprofile = "mirror"\n', encoding="utf-8")
    assert Config.load().golden_profile == "mirror"


def test_env_var_beats_xdg_user_config(user_config, tmp_path, monkeypatch) -> None:
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text('[golden]\nprofile = "mirror"\n', encoding="utf-8")
    env_file = tmp_path / "env.toml"
    env_file.write_text('[golden]\nprofile = "stock"\n', encoding="utf-8")
    monkeypatch.setenv("OMAVROOM_CONFIG", str(env_file))
    assert Config.load().golden_profile == "stock"


def test_explicit_path_beats_env(user_config, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OMAVROOM_CONFIG", str(tmp_path / "missing.toml"))
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('[golden]\nprofile = "mirror"\n', encoding="utf-8")
    assert Config.load(explicit).golden_profile == "mirror"


# -- persistence -------------------------------------------------------------
def test_set_config_value_round_trip_preserves_other_keys(user_config) -> None:
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text(
        "[host]\nheadroom_floor_mb = 999\n"
        "[seats.desktop]\nmax_seats = 3\n"
        '[golden]\nprofile = "stock"\n',
        encoding="utf-8",
    )
    coerced, path = set_config_value("golden", "profile", "mirror")
    assert coerced == "mirror"
    assert path == user_config

    text = user_config.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["golden"]["profile"] == "mirror"
    assert data["host"]["headroom_floor_mb"] == 999
    assert data["seats"]["desktop"]["max_seats"] == 3

    reloaded = Config.from_toml(user_config)
    assert reloaded.golden_profile == "mirror"
    assert reloaded.host.headroom_floor_mb == 999
    assert reloaded.seats["desktop"].max_seats == 3


def test_set_config_value_creates_user_config(user_config) -> None:
    assert not user_config.exists()
    set_config_value("golden", "profile", "mirror")
    assert user_config.exists()
    assert Config.from_toml(user_config).golden_profile == "mirror"


def test_set_config_value_rejects_bad_value_without_writing(user_config) -> None:
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text('[golden]\nprofile = "stock"\n', encoding="utf-8")
    before = user_config.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match=r"golden\.profile must be one of"):
        set_config_value("golden", "profile", "nope")
    assert user_config.read_text(encoding="utf-8") == before


# -- daemon wire method ------------------------------------------------------
def test_daemon_set_config_value_round_trip(fake_daemon, user_config) -> None:
    with fake_daemon() as pool:
        client = DaemonClient(pool.socket_path)
        try:
            result = client.set_config_value("golden", "profile", "mirror")
        finally:
            client.close()
    assert result["value"] == "mirror"
    assert result["section"] == "golden" and result["key"] == "profile"
    assert pool.manager.config.golden_profile == "mirror"
    assert tomllib.loads(user_config.read_text(encoding="utf-8"))["golden"]["profile"] == "mirror"


def test_daemon_set_config_value_invalid_is_structured(fake_daemon, user_config) -> None:
    with fake_daemon() as pool:
        client = DaemonClient(pool.socket_path)
        try:
            with pytest.raises(DaemonRequestError) as excinfo:
                client.set_config_value("golden", "profile", "nope")
        finally:
            client.close()
    assert excinfo.value.code == "invalid"
    assert not user_config.exists()


# -- CLI ---------------------------------------------------------------------
def _cli(pool, *argv):
    return cli.main(["--socket", str(pool.socket_path), *argv])


def test_cli_settings_show_includes_golden(user_config, capsys) -> None:
    assert cli.main(["settings", "show", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["golden"]["profile"] == "stock"
    assert cli.main(["settings", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["golden"]["profile"] == "stock"
    assert cli.main(["settings"]) == 0
    assert "golden: profile=stock" in capsys.readouterr().out


def test_cli_settings_set_round_trip(fake_daemon, user_config, capsys) -> None:
    with fake_daemon() as pool:
        assert _cli(pool, "settings", "set", "golden.profile", "mirror") == 0
    assert "golden.profile = 'mirror'" in capsys.readouterr().out
    assert Config.from_toml(user_config).golden_profile == "mirror"


def test_cli_settings_set_rejects_bad_value(fake_daemon, user_config, capsys) -> None:
    with fake_daemon() as pool:
        assert _cli(pool, "settings", "set", "golden.profile", "nope") == 1
    assert "invalid" in capsys.readouterr().err


def test_cli_settings_set_requires_dotted_assignment(fake_daemon, user_config, capsys) -> None:
    with fake_daemon() as pool:
        assert _cli(pool, "settings", "set", "golden", "mirror") == 2
    assert "expected <section>.<key>" in capsys.readouterr().err
