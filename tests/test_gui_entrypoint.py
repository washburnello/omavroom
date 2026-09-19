"""Phase 8 headless entry-point tests: imports + argument parsing only.

Nothing here starts Qt or touches a display, so it runs on any host.
"""

from __future__ import annotations

import importlib

from omavroom.config import Config
from omavroom.gui import app as gui_app


def test_gui_module_imports_without_starting_qt():
    module = importlib.import_module("omavroom.gui.app")
    assert hasattr(module, "run_gui")
    assert hasattr(module, "main")
    # Building the parser must not import (still less start) Qt.
    import subprocess
    import sys

    code = (
        "import sys; from omavroom.gui.app import build_parser; "
        "build_parser().parse_args(['--interval', '2']); "
        "raise SystemExit(0 if 'PySide6' not in sys.modules else 9)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()


def test_gui_parser_defaults():
    args = gui_app.build_parser().parse_args([])
    assert args.socket is None
    # No override by default: the [gui] config section supplies the cadence.
    assert args.interval is None
    assert args.screenshot_width is None


def test_gui_parser_overrides():
    args = gui_app.build_parser().parse_args(
        [
            "--socket",
            "/tmp/omavroom.sock",
            "--interval",
            "1.5",
            "--screenshot-width",
            "320",
            "--viewer",
            "vncviewer {}",
        ]
    )
    assert args.socket == "/tmp/omavroom.sock"
    assert args.interval == 1.5
    assert args.screenshot_width == 320
    assert args.viewer == "vncviewer {}"


def test_cli_gui_subcommand_parses():
    from omavroom.cli import build_parser

    args = build_parser().parse_args(["gui", "--interval", "3", "--viewer", "remote-viewer"])
    assert args.command == "gui"
    assert args.interval == 3
    assert args.viewer == "remote-viewer"


def test_cli_gui_dispatch_is_registered():
    # A missing PySide6 would surface as a clean exit-1 error; either way the
    # subcommand must be recognised (not "unknown command").
    import omavroom.cli as cli
    from omavroom.cli import main

    assert "gui" in cli._COMMAND_HELP
    assert callable(main)


# FIX 5: CLI overrides are clamped to the same floors as the config.
def test_effective_capture_overrides_clamp_to_config_floors():
    cfg = Config.default()
    # A too-small width override is raised to the shared floor (64), and the
    # wall cadence never falls below the focused cadence.
    wall, width = gui_app.effective_capture_overrides(cfg, interval=None, screenshot_width=1)
    assert width == 64
    assert wall == cfg.gui.wall_interval_s

    wall, width = gui_app.effective_capture_overrides(cfg, interval=0, screenshot_width=None)
    assert wall == cfg.gui.focused_interval_s
    assert width == cfg.gui.thumbnail_width


def test_effective_capture_overrides_ceiling_and_focused_cap():
    cfg = Config.default()
    wall, width = gui_app.effective_capture_overrides(cfg, interval=99, screenshot_width=99999)
    assert wall == 99
    # The thumbnail can never exceed the focused width, nor the hard ceiling.
    assert width == min(4096, cfg.gui.focused_width)
