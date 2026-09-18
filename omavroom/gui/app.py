"""``omavroom-gui`` / ``omavroom gui`` entry point (Phase 8).

The module is import-safe without PySide6: PySide6 is imported lazily inside
:func:`run_gui`, so a headless host can still ``import omavroom.gui.app`` and
parse ``--help``. When PySide6 is missing, :func:`run_gui` raises
:class:`SystemExit` with a clear message.

Threading
---------
:class:`~omavroom.gui.backend.PollWorker` is moved to a :class:`QThread` that
owns every blocking daemon call. The GUI thread only receives queued snapshot
signals, so a slow daemon never freezes the wall.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from omavroom.config import Config

DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_SCREENSHOT_WIDTH = 1024
VIEWER_ENV_VAR = "OMAVROOM_VIEWER"


def build_parser() -> argparse.ArgumentParser:
    """Build the ``omavroom-gui`` argument parser (no PySide6 needed)."""
    parser = argparse.ArgumentParser(
        prog="omavroom-gui",
        description="Native (Qt6/QML) Command Center for the omavroom seat pool.",
    )
    parser.add_argument("--socket", default=None, help="override the daemon Unix socket path")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help="screenshot/status polling seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--screenshot-width",
        type=int,
        default=DEFAULT_SCREENSHOT_WIDTH,
        help="downscale cap for desktop thumbnails (default: %(default)s)",
    )
    parser.add_argument(
        "--viewer",
        default=os.environ.get(VIEWER_ENV_VAR),
        help="viewer command for click-to-peek ({}=endpoint); never auto-launched",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("OMAVROOM_CONFIG"),
        help="path to the effective omavroom TOML config (env: OMAVROOM_CONFIG)",
    )
    return parser


def _load_config(path: str | None) -> Config:
    try:
        return Config.load(path)
    except (OSError, ValueError) as exc:
        print(f"omavroom-gui: cannot load config {path!r}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def run_gui(
    *,
    socket_path: str | Path | None = None,
    interval: float = DEFAULT_POLL_INTERVAL_S,
    screenshot_width: int = DEFAULT_SCREENSHOT_WIDTH,
    viewer: str | None = None,
    config: Config | None = None,
) -> int:
    """Construct and run the Qt application; returns the process exit code."""
    try:
        from PySide6.QtCore import QMetaObject, Qt, QThread, QUrl
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
    except ImportError as exc:  # pragma: no cover - depends on install
        raise SystemExit(
            f"omavroom-gui requires PySide6 ({exc}). Install with 'uv sync' or 'uv add pyside6'."
        ) from exc

    from omavroom.gui.backend import SHUTDOWN_WAIT_MS, PollWorker, WallBackend

    application = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    application.setApplicationName("omavroom")
    application.setApplicationDisplayName("omavroom Command Center")

    effective_config = config or Config.load()
    backend = WallBackend(
        effective_config,
        poll_interval_s=interval,
        viewer_command=viewer or os.environ.get(VIEWER_ENV_VAR),
    )
    worker = PollWorker(
        socket_path,
        poll_interval_s=interval,
        screenshot_max_width=screenshot_width,
    )
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    worker.snapshotReady.connect(backend.apply_payload)
    worker.actionResult.connect(backend.on_action_result)
    backend.requestPoll.connect(worker.poll_once)
    backend.requestAction.connect(worker.perform_action)
    backend.requestAdmission.connect(worker.set_admission)
    backend.requestPollInterval.connect(worker.set_interval)
    backend.requestScreenshotWidth.connect(worker.set_screenshot_width)

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    qml_path = Path(__file__).with_name("qml") / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_path)))
    if not engine.rootObjects():
        print(f"omavroom-gui: failed to load {qml_path}", file=sys.stderr)
        return 1

    def _shutdown() -> None:
        """Interrupt the worker, then stop/join it within a bounded wait.

        A bare ``BlockingQueuedConnection`` stop would wait for the worker's
        event loop, which can be stuck for up to ``POLL_REQUEST_TIMEOUT_S``
        (8 s) in a daemon read or ``ACTION_TIMEOUT_S`` (300 s) in a recovery
        job. So the first step is a lock-free interrupt (sets the flag and
        force-closes the socket), which unblocks any read *and* makes an
        in-progress ``_await_job`` observe the flag within ~100 ms. Only then
        is a blocking ``stop`` invoke safe: it runs on the worker thread (so
        the timer is stopped on its owner) and returns promptly. The join is
        still hard-capped so a truly wedged daemon cannot hang the close.
        """
        if not thread.isRunning():
            return
        worker.interrupt()
        QMetaObject.invokeMethod(worker, "stop", Qt.ConnectionType.BlockingQueuedConnection)
        thread.quit()
        thread.wait(SHUTDOWN_WAIT_MS)

    application.aboutToQuit.connect(_shutdown)
    thread.start()
    try:
        return application.exec()
    finally:
        _shutdown()


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``omavroom-gui`` console script."""
    args = build_parser().parse_args(argv)
    return run_gui(
        socket_path=args.socket,
        interval=args.interval,
        screenshot_width=args.screenshot_width,
        viewer=args.viewer,
        config=_load_config(args.config),
    )


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_SCREENSHOT_WIDTH",
    "VIEWER_ENV_VAR",
    "build_parser",
    "main",
    "run_gui",
]
