"""Phase 8A FIX 1: shutdown must never block the GUI thread on a hung daemon.

A daemon that accepts the socket connection but never replies used to pin the
worker inside an 8 s ``readline`` (``POLL_REQUEST_TIMEOUT_S``); a recovery job
could pin it for ``ACTION_TIMEOUT_S`` (300 s). The old ``_shutdown`` used a
``BlockingQueuedConnection`` stop, so closing the app waited on that worker
loop and Hyprland showed "Application Not Responding".

These tests build a black-hole Unix socket (accept + never answer), let the
worker block on it, then run the app's shutdown sequence and assert it returns
in a small bound and leaves no running thread.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QMetaObject, Qt, QThread  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from omavroom.gui.backend import PollWorker  # noqa: E402

SHUTDOWN_BOUND_S = 2.0


def _qapp() -> QGuiApplication:
    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication([])
    return app


@pytest.fixture
def blackhole(tmp_path):
    """A Unix socket that accepts connections but never sends a reply."""
    sock_path = tmp_path / "blackhole.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(8)
    stop = threading.Event()
    conns: list[socket.socket] = []

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                server.settimeout(0.1)
                conn, _ = server.accept()
                conns.append(conn)
            except TimeoutError:
                continue
            except OSError:
                break

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(path=sock_path, server=server, conns=conns)
    finally:
        stop.set()
        server.close()
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass


def _start_worker(socket_path) -> tuple[PollWorker, QThread]:
    worker = PollWorker(socket_path, poll_interval_s=0.3)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    thread.start()
    # Give it time to accept the black-hole connection and block in readline.
    time.sleep(0.4)
    return worker, thread


def _app_shutdown(worker: PollWorker, thread: QThread) -> None:
    """The exact sequence ``run_gui._shutdown`` uses."""
    if not thread.isRunning():
        return
    worker.interrupt()
    QMetaObject.invokeMethod(worker, "stop", Qt.ConnectionType.BlockingQueuedConnection)
    thread.quit()
    thread.wait(1500)


def test_interrupt_unblocks_a_hung_socket_read(blackhole):
    """``interrupt`` force-closes the socket, so a blocked read returns fast.

    ``interrupt`` deliberately does not call ``thread.quit()`` (that is the
    app's job); here we only prove the *read* unblocks quickly, then quit.
    """
    app = _qapp()
    worker, thread = _start_worker(blackhole.path)
    assert thread.isRunning()
    started = time.monotonic()
    worker.interrupt()  # this is the call that used to block on the client lock
    elapsed = time.monotonic() - started
    assert elapsed < SHUTDOWN_BOUND_S, f"interrupt took {elapsed:.2f}s"
    thread.quit()
    thread.wait(1500)
    assert not thread.isRunning()
    app.processEvents()


def test_full_shutdown_sequence_returns_within_bound(blackhole):
    app = _qapp()
    worker, thread = _start_worker(blackhole.path)
    assert thread.isRunning()
    started = time.monotonic()
    _app_shutdown(worker, thread)
    elapsed = time.monotonic() - started
    app.processEvents()
    assert elapsed < SHUTDOWN_BOUND_S, f"shutdown took {elapsed:.2f}s"
    assert not thread.isRunning(), "worker thread still running after shutdown"


def test_shutdown_is_idempotent(blackhole):
    app = _qapp()
    worker, thread = _start_worker(blackhole.path)
    _app_shutdown(worker, thread)
    # A second shutdown (aboutToQuit and finally both call it) must not raise
    # and must return immediately.
    started = time.monotonic()
    _app_shutdown(worker, thread)
    elapsed = time.monotonic() - started
    app.processEvents()
    assert elapsed < 0.5
    assert not thread.isRunning()


def test_stop_flag_aborts_poll_without_emitting(blackhole):
    """A poll that starts after a stop request emits nothing and returns."""
    app = _qapp()
    worker = PollWorker(blackhole.path, poll_interval_s=0.3)
    snapshots: list = []
    worker.snapshotReady.connect(lambda payload: snapshots.append(payload))
    worker.interrupt()
    # Called directly on this (test) thread; must be a no-op.
    worker.poll_once()
    app.processEvents()
    assert snapshots == []
