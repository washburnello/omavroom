"""Phase 4 config schema: per-type resources, images, admission settings."""

from __future__ import annotations

import pytest

from omavroom.config import (
    DEFAULT_IMAGE,
    AdmissionConfig,
    Config,
    ExportConfig,
    PrewarmConfig,
    ResourceConfig,
    SeatTypeConfig,
)


def test_resources_defaults_match_real_box() -> None:
    cfg = Config.default()
    desktop = cfg.resources["desktop"]
    terminal = cfg.resources["terminal"]
    assert (desktop.cpu_vcpus, desktop.memory_mb, desktop.overlay_max_gb) == (2, 4096, 20)
    assert (terminal.cpu_vcpus, terminal.memory_mb, terminal.overlay_max_gb) == (2, 2048, 10)


def test_exec_max_runtime_accepts_zero() -> None:
    from omavroom.config import ExecConfig

    assert ExecConfig().max_runtime_s == 0
    assert ExecConfig(max_runtime_s=0).max_runtime_s == 0
    assert Config.validate_value("exec", "max_runtime_s", 0) == 0
    with pytest.raises(ValueError, match="max_runtime_s must be >= 0"):
        ExecConfig(max_runtime_s=-1)


def test_seat_default_image_is_set() -> None:
    cfg = Config.default()
    assert cfg.seats["desktop"].image == "golden-omarchy"
    assert cfg.seats["terminal"].image == DEFAULT_IMAGE
    assert cfg.image_for("desktop") == "golden-omarchy"


def test_golden_omarchy_registered_with_desktop_fallback() -> None:
    cfg = Config.default()
    assert cfg.images["golden-omarchy"].seat_type == "desktop"
    assert cfg.golden_for("desktop").name == "golden-omarchy.qcow2"
    # the stock goldens stay registered so switching back is one line
    assert cfg.images["golden-desktop"].seat_type == "desktop"
    assert cfg.golden_for("desktop", "golden-desktop").name == "golden-desktop.qcow2"
    assert cfg.golden_for("desktop", "omavroom-base").name == "golden-desktop.qcow2"


def test_admission_defaults() -> None:
    cfg = Config.default()
    assert cfg.admission.dynamic is True
    assert cfg.admission.override == "auto"


def test_toml_overrides_image_resources_and_admission(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(
        "[seats.desktop]\n"
        'image = "custom-desktop"\n'
        "max_seats = 3\n"
        "[resources.terminal]\n"
        "memory_mb = 1024\n"
        "[admission]\n"
        "dynamic = false\n"
        'override = "allow"\n',
        encoding="utf-8",
    )
    cfg = Config.from_toml(cfg_file)
    assert cfg.seats["desktop"].image == "custom-desktop"
    assert cfg.seats["desktop"].max_seats == 3
    assert cfg.seats["terminal"].image == DEFAULT_IMAGE
    assert cfg.resources["terminal"].memory_mb == 1024
    assert cfg.resources["terminal"].cpu_vcpus == 2
    assert cfg.admission.dynamic is False
    assert cfg.admission.override == "allow"


def test_resources_for_helper() -> None:
    cfg = Config.default()
    assert cfg.resources_for("terminal").memory_mb == 2048


def test_unknown_resource_key_rejected(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text("[resources.desktop]\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"unknown keys in \[resources\.desktop\].*bogus"):
        Config.from_toml(cfg_file)


def test_unknown_resource_seat_type_rejected(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text("[resources.watch]\nmemory_mb = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown seat types"):
        Config.from_toml(cfg_file)


def test_resources_must_be_a_table(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text("[resources]\ndesktop = 5\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[resources\.desktop\] must be a table"):
        Config.from_toml(cfg_file)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("[seats.desktop]\nimage = 5\n", r"seats\.desktop\.image must be a non-empty string"),
        ('[seats.desktop]\nimage = ""\n', r"seats\.desktop\.image must be a non-empty string"),
        ('[admission]\ndynamic = "yes"\n', r"admission\.dynamic must be a boolean"),
        ('[admission]\noverride = "sometimes"\n', r"admission\.override"),
        ("[resources.desktop]\nmemory_mb = 0\n", r"memory_mb must be >= 1"),
        ('[resources.desktop]\nmemory_mb = "lots"\n', r"resources\.desktop\.memory_mb"),
    ],
)
def test_bad_phase4_values_rejected(tmp_path, body, match) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        Config.from_toml(cfg_file)


def test_dataclasses_validate_directly() -> None:
    with pytest.raises(ValueError):
        SeatTypeConfig(image="")
    with pytest.raises(ValueError):
        ResourceConfig(cpu_vcpus=0)
    with pytest.raises(ValueError):
        AdmissionConfig(override="nope")
    with pytest.raises(ValueError):
        AdmissionConfig(dynamic=1)  # type: ignore[arg-type]


def test_export_and_prewarm_defaults() -> None:
    cfg = Config.default()
    assert cfg.export.max_files_changed == 200
    assert cfg.export.max_insertions == 5000
    assert cfg.export.max_deletions == 5000
    assert cfg.export.protected_paths == (".git/", ".github/")
    assert cfg.export.approval_required is False
    assert cfg.prewarm.max_retries == 1
    assert cfg.prewarm.backoff_s == 60


def test_export_toml_override(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(
        "[export]\n"
        "max_files_changed = 3\n"
        "max_deletions = 7\n"
        'protected_paths = ["secrets/", ".env"]\n'
        "approval_required = true\n"
        "[prewarm]\n"
        "max_retries = 4\n"
        "backoff_s = 5\n",
        encoding="utf-8",
    )
    cfg = Config.from_toml(cfg_file)
    assert cfg.export.max_files_changed == 3
    assert cfg.export.max_insertions == 5000  # untouched default
    assert cfg.export.max_deletions == 7
    assert cfg.export.protected_paths == ("secrets/", ".env")
    assert cfg.export.approval_required is True
    assert cfg.prewarm.max_retries == 4
    assert cfg.prewarm.backoff_s == 5


def test_export_config_matches_protected() -> None:
    cfg = ExportConfig(protected_paths=("secrets/", ".env"))
    assert cfg.matches_protected("secrets/token.txt") == "secrets/"
    assert cfg.matches_protected(".env") == ".env"
    assert cfg.matches_protected("src/app.py") is None


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("[export]\nmax_files_changed = -1\n", r"max_files_changed must be >= 0"),
        ('[export]\nprotected_paths = "secrets/"\n', r"export\.protected_paths"),
        ('[export]\nprotected_paths = [""]\n', r"export\.protected_paths"),
        ('[export]\napproval_required = "yes"\n', r"export\.approval_required"),
        ("[export]\nbogus = 1\n", r"unknown keys in \[export\].*bogus"),
        ("[prewarm]\nmax_retries = -1\n", r"max_retries must be >= 0"),
        ("[prewarm]\nbackoff_s = -2\n", r"backoff_s must be >= 0"),
        ("[prewarm]\nbogus = 1\n", r"unknown keys in \[prewarm\].*bogus"),
    ],
)
def test_bad_export_and_prewarm_values_rejected(tmp_path, body, match) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        Config.from_toml(cfg_file)


def test_prewarm_config_validates_directly() -> None:
    with pytest.raises(ValueError):
        PrewarmConfig(max_retries=-1)
    with pytest.raises(ValueError):
        PrewarmConfig(backoff_s=-1)
