"""Native Qt6/QML Command Center for omavroom (Phase 8).

Three layers, deliberately separated:

- :mod:`omavroom.gui.viewmodel` — pure, Qt-free read model (unit-testable).
- :mod:`omavroom.gui.backend` — thin Qt bridge + background poll worker.
- :mod:`omavroom.gui.app` — ``omavroom-gui`` entry point and QML loading.

The QML itself lives in :mod:`omavroom.gui.qml`. Importing this package is
PySide6-free; PySide6 is imported only when :func:`omavroom.gui.app.run_gui`
is called.
"""

from __future__ import annotations

from omavroom.gui.app import build_parser, main, run_gui

__all__ = ["build_parser", "main", "run_gui"]
