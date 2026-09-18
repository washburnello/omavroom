"""Phase 8 headless entry-point tests: imports + argument parsing only.

Nothing here starts Qt or touches a display, so it runs on any host.
"""

from __future__ import annotations

import importlib

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
    assert args.interval == gui_app.DEFAULT_POLL_INTERVAL_S
    assert args.screenshot_width == gui_app.DEFAULT_SCREENSHOT_WIDTH


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
