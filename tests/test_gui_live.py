"""Backend live-mode wiring tests (package C), Qt offscreen.

With an injected fake frame source, ``live_mode="vnc"`` must start the stream
for the focused desktop seat, advance ``liveRevision`` from the source, stop on
clear/focus change, and fall back to stills on error. The default stills mode
must not touch the source at all.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import (  # noqa: E402
    QCoreApplication,
    QEvent,
    QEventLoop,
    QObject,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuick import QQuickWindow  # noqa: E402

from omavroom.config import Config  # noqa: E402
from omavroom.gui.backend import WallBackend  # noqa: E402
from omavroom.gui.frames import FrameImageProvider  # noqa: E402

QML_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "omavroom", "gui", "qml")


class FakeFrameSource:
    """A scripted :class:`FrameSource` recording start/stop and revisions."""

    def __init__(self) -> None:
        self.started: dict[int, tuple[int, str | None]] = {}
        self.stopped: list[int] = []
        self.revisions: dict[int, int] = {}
        self.errors: dict[int, str | None] = {}
        self._image = QImage(4, 2, QImage.Format.Format_RGB32)

    def start(self, seat_id: int, width: int, endpoint: str | None = None) -> None:
        self.started[int(seat_id)] = (int(width), endpoint)
        self.revisions.setdefault(int(seat_id), 0)
        self.errors.pop(int(seat_id), None)

    def stop(self, seat_id: int) -> None:
        self.stopped.append(int(seat_id))

    def frame(self, seat_id: int):
        return self._image

    def revision(self, seat_id: int) -> int:
        return self.revisions.get(int(seat_id), 0)

    def error(self, seat_id: int) -> str | None:
        return self.errors.get(int(seat_id))

    def stop_all(self) -> None:
        self.stopped.extend(self.started)


def _qapp() -> QGuiApplication:
    app = QGuiApplication.instance()
    return app if app is not None else QGuiApplication([])


def _config(*, desktop: int = 1, terminal: int = 1) -> Config:
    cfg = Config.default()
    cfg.seats["desktop"].max_seats = desktop
    cfg.seats["terminal"].max_seats = terminal
    return cfg


def _seat(seat_id: int, name: str, seat_type: str = "desktop") -> dict:
    return {
        "id": seat_id,
        "name": name,
        "seat_type": seat_type,
        "state": "ready",
        "vm_name": f"fake://{name}",
        "image": "omavroom-base",
        "agent_label": None,
        "last_error": None,
        "needs_attention": False,
    }


def _payload(seats: list[dict], per_type: dict) -> dict:
    return {
        "ok": True,
        "status": {
            "free_ram_mb": 8192,
            "headroom_floor_mb": 2048,
            "admission_override": "auto",
            "seats": seats,
            "queue": [],
            "per_type": per_type,
        },
        "execs": {},
        "screenshots": {},
    }


PER_TYPE = {"desktop": {"max_seats": 1}, "terminal": {"max_seats": 1}}


def _backend_with_focus(*, live_mode: str = "vnc"):
    source = FakeFrameSource()
    backend = WallBackend(_config(), frame_source=source)
    if live_mode != "stills":
        backend.setGuiSetting("live_mode", live_mode)
    endpoints: list[tuple[int, str]] = []
    backend.requestLiveEndpoint.connect(
        lambda seat_id: (
            endpoints.append((seat_id, f"vnc://127.0.0.1:{5900 + seat_id}")),
            backend.on_action_result(
                "live-endpoint", True, "ok", seat_id, f"vnc://127.0.0.1:{5900 + seat_id}"
            ),
        )
    )
    backend.apply_payload(_payload([_seat(5, "desktop-1")], PER_TYPE))
    return backend, source, endpoints


# --------------------------------------------------------------------------
# start / stop lifecycle
# --------------------------------------------------------------------------
def test_vnc_mode_starts_stream_on_focus_and_stops_on_clear():
    backend, source, endpoints = _backend_with_focus(live_mode="vnc")
    assert source.started == {}

    backend.toggleFocus("desktop-0")
    assert endpoints == [(5, "vnc://127.0.0.1:5905")]
    assert 5 in source.started
    assert backend.liveSeatId == 5

    backend.clearFocus()
    assert source.stopped == [5]
    assert backend.liveSeatId == -1


def test_stills_mode_never_touches_the_source():
    backend, source, endpoints = _backend_with_focus(live_mode="stills")
    backend.toggleFocus("desktop-0")
    assert source.started == {} and endpoints == []
    assert backend.liveSeatId == -1


def test_terminal_focus_does_not_start_vnc():
    source = FakeFrameSource()
    backend = WallBackend(_config(desktop=0, terminal=1), frame_source=source)
    backend.setGuiSetting("live_mode", "vnc")
    backend.apply_payload(_payload([_seat(9, "terminal-1", "terminal")], PER_TYPE))
    backend.toggleFocus("terminal-0")
    assert source.started == {}
    assert backend.liveSeatId == -1


def test_revision_advances_and_emits_live_changed():
    backend, source, _ = _backend_with_focus(live_mode="vnc")
    backend.toggleFocus("desktop-0")
    seen: list[int] = []
    backend.liveChanged.connect(lambda: seen.append(backend.liveRevision))
    source.revisions[5] = 3
    backend._poll_live()
    assert backend.liveRevision == 3
    assert seen == [3]
    backend._poll_live()  # unchanged revision does not re-emit
    assert seen == [3]


def test_vnc_error_falls_back_to_stills_with_a_notice():
    backend, source, _ = _backend_with_focus(live_mode="vnc")
    backend.toggleFocus("desktop-0")
    source.errors[5] = "handshake failed"
    backend._poll_live()
    assert backend.liveSeatId == -1
    assert 5 in source.stopped
    assert "handshake failed" in backend.liveModeNotice
    assert "using stills" in backend.lastMessage


def test_missing_endpoint_falls_back_without_starting():
    source = FakeFrameSource()
    backend = WallBackend(_config(), frame_source=source)
    backend.setGuiSetting("live_mode", "vnc")
    backend.requestLiveEndpoint.connect(
        lambda seat_id: backend.on_action_result("live-endpoint", False, "no endpoint", seat_id, "")
    )
    backend.apply_payload(_payload([_seat(5, "desktop-1")], PER_TYPE))
    backend.toggleFocus("desktop-0")
    assert source.started == {}
    assert backend.liveSeatId == -1
    assert "no endpoint" in backend.liveModeNotice


def test_focus_switch_stops_old_seat_and_streams_new_one():
    source = FakeFrameSource()
    backend = WallBackend(_config(desktop=2, terminal=0), frame_source=source)
    backend.setGuiSetting("live_mode", "vnc")
    backend.requestLiveEndpoint.connect(
        lambda seat_id: backend.on_action_result("live-endpoint", True, "ok", seat_id, "vnc://x:1")
    )
    per_type = {"desktop": {"max_seats": 2}, "terminal": {"max_seats": 0}}
    backend.apply_payload(_payload([_seat(5, "desktop-1"), _seat(6, "desktop-2")], per_type))
    backend.toggleFocus("desktop-0")
    assert backend.liveSeatId == 5
    backend.toggleFocus("desktop-1")
    assert 5 in source.stopped
    assert backend.liveSeatId == 6


def test_shutdown_live_stops_everything():
    backend, source, _ = _backend_with_focus(live_mode="vnc")
    backend.toggleFocus("desktop-0")
    backend.shutdown_live()
    assert backend.liveSeatId == -1
    assert 5 in source.stopped


# --------------------------------------------------------------------------
# QML delivery: the focused tile renders the provider URL
# --------------------------------------------------------------------------
def _pump(app: QGuiApplication, ms: int = 40) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def _all_items(node) -> list[QObject]:
    out: list[QObject] = []

    def walk(item) -> None:
        for child in item.childItems():
            out.append(child)
            walk(child)

    walk(node)
    return out


def _find(root, prefix: str) -> list[QObject]:
    base = root.contentItem() if hasattr(root, "contentItem") else root
    return [i for i in _all_items(base) if (i.objectName() or "").startswith(prefix)]


def test_focused_tile_uses_image_provider_url():
    app = _qapp()
    backend, source, _ = _backend_with_focus(live_mode="vnc")
    engine = QQmlApplicationEngine()
    provider = FrameImageProvider(backend.frame_source)
    engine.rootContext().setContextProperty("backend", backend)
    engine.addImageProvider("omavroom", provider)
    engine.load(QUrl.fromLocalFile(os.path.join(QML_DIR, "Main.qml")))
    for _ in range(5):
        app.processEvents()
    try:
        root = engine.rootObjects()[0]
        backend.toggleFocus("desktop-0")
        source.revisions[5] = 2
        backend._poll_live()
        QQuickWindow.grabWindow(root)
        _pump(app)
        tiles = {t.objectName(): t for t in _find(root, "slotTile-")}
        focused = tiles["slotTile-desktop-0"]
        assert focused.property("live") is True
        assert focused.property("liveSource") == "image://omavroom/5?v=2"
    finally:
        backend.shutdown_live()
        engine.deleteLater()
        # Force the deferred engine delete before ``backend`` is collected, so
        # QML bindings never re-evaluate against a null context property.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
