"""Phase 8 poll-worker tests against the in-process fake-provisioner daemon.

No VMs, no display: ``QT_QPA_PLATFORM=offscreen`` plus the shared
``fake_daemon`` fixture. These exercise the real cross-thread path (worker
thread -> queued signal -> backend state) and the daemon-down/retry banner.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEventLoop, QThread, QTimer  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from omavroom.client import DaemonClient  # noqa: E402
from omavroom.config import Config  # noqa: E402
from omavroom.gui.backend import PollWorker, WallBackend  # noqa: E402


def _qapp() -> QGuiApplication:
    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication([])
    return app


def _config(*, desktop: int = 1, terminal: int = 2) -> Config:
    cfg = Config.default()
    cfg.seats["desktop"].max_seats = desktop
    cfg.seats["terminal"].max_seats = terminal
    return cfg


def _pump(app: QGuiApplication, ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def _run_worker(socket_path):
    app = _qapp()
    backend = WallBackend(_config(desktop=0, terminal=1))
    worker = PollWorker(socket_path, poll_interval_s=0.2)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    worker.snapshotReady.connect(backend.apply_payload)
    thread.start()
    _pump(app, 600)
    return app, backend, worker, thread


def _stop(app, worker, thread):
    from PySide6.QtCore import QMetaObject, Qt

    if thread.isRunning():
        QMetaObject.invokeMethod(worker, "stop", Qt.ConnectionType.BlockingQueuedConnection)
        thread.quit()
        thread.wait(5000)
    app.processEvents()


def _ready(pool, agent, seat_type="terminal", **kwargs):
    client = DaemonClient(pool.socket_path)
    try:
        view = client.request_seat(agent, seat_type, **kwargs).wait_ready(timeout=5)
        return view["seat"]
    finally:
        client.close()


def test_pollworker_snapshot_against_fake_daemon(fake_daemon):
    cfg = _config(desktop=0, terminal=1)
    with fake_daemon(config=cfg) as pool:
        seat = _ready(pool, "alice", "terminal", project="api")
        client = DaemonClient(pool.socket_path)
        try:
            client.exec_start(seat["id"], "e1", label="build", command="echo hi")
            client.exec_finish(seat["id"], "e1", exit_code=0, stdout="guest output\n")
        finally:
            client.close()

        app, backend, worker, thread = _run_worker(pool.socket_path)
        try:
            assert backend.daemonOk is True
            assert backend.slotKeys == ["terminal-0"]
            data = backend.slotAt(0)
            assert data["occupied"] is True
            assert data["agent"] == "alice"
            assert data["project"] == "api"
            assert "guest output" in data["terminal_text"]
        finally:
            _stop(app, worker, thread)


def test_pollworker_marks_daemon_down_then_recovers(tmp_path, fake_daemon):
    cfg = _config(desktop=0, terminal=1)
    # First poll against a missing socket: the banner state.
    app, backend, worker, thread = _run_worker(tmp_path / "missing.sock")
    try:
        assert backend.daemonOk is False
        assert "not running" in backend.daemonMessage
        assert backend.slotKeys == ["terminal-0"]  # wall still present (from config)
        assert backend.slotAt(0)["occupied"] is False
    finally:
        _stop(app, worker, thread)

    # Now a live daemon: retry clears the banner.
    with fake_daemon(config=cfg) as pool:
        _ready(pool, "bob", "terminal")
        app, backend, worker, thread = _run_worker(pool.socket_path)
        try:
            assert backend.daemonOk is True
            assert backend.slotAt(0)["agent"] == "bob"
        finally:
            _stop(app, worker, thread)


def test_pollworker_retry_daemon_action_repolls(fake_daemon):
    cfg = _config(desktop=0, terminal=1)
    with fake_daemon(config=cfg) as pool:
        _ready(pool, "carol", "terminal")
        app, backend, worker, thread = _run_worker(pool.socket_path)
        try:
            backend.retryDaemon()
            _pump(app, 400)
            assert backend.daemonOk is True
            assert backend.slotAt(0)["agent"] == "carol"
        finally:
            _stop(app, worker, thread)
