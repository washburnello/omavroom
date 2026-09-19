"""``[gui]`` config: defaults, strict parsing/validation and persistence."""

from __future__ import annotations

import pytest

from omavroom.config import (
    DEFAULT_LIVE_MODE,
    Config,
    GuiConfig,
    set_config_value,
    set_config_values,
)


def test_gui_defaults() -> None:
    gui = Config.default().gui
    assert gui.thumbnail_width == 480
    assert gui.focused_width == 1024
    assert gui.focused_interval_s == 0.5
    assert gui.wall_interval_s == 2.0
    assert gui.live_mode == DEFAULT_LIVE_MODE == "stills"


def test_gui_toml_overrides(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(
        "[gui]\n"
        "thumbnail_width = 320\n"
        "focused_width = 1600\n"
        "focused_interval_s = 0.25\n"
        "wall_interval_s = 4\n"
        'live_mode = "vnc"\n',
        encoding="utf-8",
    )
    cfg = Config.from_toml(cfg_file)
    assert cfg.gui.thumbnail_width == 320
    assert cfg.gui.focused_width == 1600
    assert cfg.gui.focused_interval_s == 0.25
    assert cfg.gui.wall_interval_s == 4.0  # integral int accepted for a float field
    assert cfg.gui.live_mode == "vnc"


def test_gui_partial_toml_keeps_other_defaults(tmp_path) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text("[gui]\nthumbnail_width = 256\n", encoding="utf-8")
    gui = Config.from_toml(cfg_file).gui
    assert gui.thumbnail_width == 256
    assert gui.focused_width == 1024
    assert gui.focused_interval_s == 0.5


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("[gui]\nbogus = 1\n", r"unknown keys in \[gui\]"),
        ('[gui]\nlive_mode = "hologram"\n', r"live_mode must be one of"),
        ("[gui]\nlive_mode = 5\n", r"gui\.live_mode"),
        ("[gui]\nthumbnail_width = 4\n", r"thumbnail_width must be an integer in 64"),
        ("[gui]\nthumbnail_width = 5.5\n", r"thumbnail_width must be an integer"),
        ("[gui]\nfocused_width = 100\n", r"focused_width must be >= thumbnail_width"),
        ("[gui]\nfocused_interval_s = 0\n", r"focused_interval_s must be a number >= 0.05"),
        ('[gui]\nfocused_interval_s = "fast"\n', r"gui\.focused_interval_s"),
        ("[gui]\nwall_interval_s = 0.1\n", r"wall_interval_s must be >= focused_interval_s"),
    ],
)
def test_gui_bad_values_rejected(tmp_path, body, match) -> None:
    cfg_file = tmp_path / "omavroom.toml"
    cfg_file.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        Config.from_toml(cfg_file)


def test_gui_config_validates_directly() -> None:
    with pytest.raises(ValueError):
        GuiConfig(focused_width=100)
    with pytest.raises(ValueError):
        GuiConfig(wall_interval_s=0.1)
    with pytest.raises(ValueError):
        GuiConfig(live_mode="nope")


def test_validate_value_accepts_gui_keys() -> None:
    assert Config.validate_value("gui", "thumbnail_width", 640) == 640
    assert Config.validate_value("gui", "focused_interval_s", 1) == 1.0
    with pytest.raises(ValueError, match="unknown key in \\[gui\\]"):
        Config.validate_value("gui", "bogus", 1)


def test_set_config_value_persists_gui(tmp_path) -> None:
    path = tmp_path / "config.toml"
    coerced, written = set_config_value("gui", "live_mode", "vnc", path=path)
    assert coerced == "vnc"
    assert written == path
    # A second write preserves the first and still round-trips through the
    # strict loader.
    set_config_value("gui", "thumbnail_width", 320, path=path)
    cfg = Config.from_toml(path)
    assert cfg.gui.live_mode == "vnc"
    assert cfg.gui.thumbnail_width == 320


def test_set_config_value_rejects_bad_gui(tmp_path) -> None:
    with pytest.raises(ValueError):
        set_config_value("gui", "live_mode", "hologram", path=tmp_path / "config.toml")
    # A focused width below the thumbnail width is a cross-field conflict.
    with pytest.raises(ValueError):
        set_config_value("gui", "focused_width", 100, path=tmp_path / "config.toml")


def test_set_config_values_applies_a_combined_change_atomically(tmp_path) -> None:
    path = tmp_path / "config.toml"
    coerced, written = set_config_values(
        "gui", {"thumbnail_width": 1600, "focused_width": 2048}, path=path
    )
    assert written == path
    assert coerced == {"thumbnail_width": 1600, "focused_width": 2048}
    cfg = Config.from_toml(path)
    assert (cfg.gui.thumbnail_width, cfg.gui.focused_width) == (1600, 2048)

    # An invalid combined change is rejected without touching the file.
    with pytest.raises(ValueError):
        set_config_values("gui", {"thumbnail_width": 2048, "focused_width": 1600}, path=path)
    cfg = Config.from_toml(path)
    assert (cfg.gui.thumbnail_width, cfg.gui.focused_width) == (1600, 2048)


def test_config_set_values_is_atomic_locally() -> None:
    cfg = Config.default()
    with pytest.raises(ValueError):
        cfg.set_values("gui", {"thumbnail_width": 1600, "focused_width": 480})
    assert (cfg.gui.thumbnail_width, cfg.gui.focused_width) == (480, 1024)
    cfg.set_values("gui", {"focused_interval_s": 1.5, "wall_interval_s": 3.0})
    assert (cfg.gui.focused_interval_s, cfg.gui.wall_interval_s) == (1.5, 3.0)
