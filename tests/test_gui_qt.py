"""Phase 8 Qt offscreen smoke tests.

``QT_QPA_PLATFORM=offscreen`` drives a real Qt event loop with no display.
The tests construct the backend + QML window, feed the wall a *fake* snapshot
(no daemon socket, no VM), then **force a real render pass** and assert on the
actual visual ``SlotTile``/``QueuePanel``/``AttentionPanel`` delegates.

Why the render pass matters: a QML ``Repeater``/``ListView`` populates its
visual delegate group lazily, when the item is first painted. Without a render
the delegates do not exist and a broken ``SlotTile.qml`` would pass. Calling
``QQuickWindow.grabWindow()`` on the offscreen window forces that pass. QML
visual items are parented via the *scene graph*, not the QObject tree, so the
delegates are found by recursing ``QQuickItem.childItems()`` — ``findChildren``
does not see them.

PySide6 is a hard dependency, but these tests skip cleanly where it cannot be
imported so the rest of the suite stays green on a host without Qt.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEventLoop, QObject, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuick import QQuickWindow  # noqa: E402

from omavroom.config import Config  # noqa: E402
from omavroom.gui.backend import PollWorker, WallBackend  # noqa: E402
from omavroom.gui.capture import CapturePlanner  # noqa: E402
from omavroom.gui.viewmodel import ATTENTION_ACTIONS  # noqa: E402

QML_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "omavroom", "gui", "qml")


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


def _status(*, seats, per_type):
    return {
        "free_ram_mb": 8192,
        "headroom_floor_mb": 2048,
        "admission_override": "auto",
        "seats": seats,
        "queue": [],
        "per_type": per_type,
    }


def _seat(seat_id, name, seat_type="terminal", state="ready", agent=None, **extra):
    seat = {
        "id": seat_id,
        "name": name,
        "seat_type": seat_type,
        "state": state,
        "vm_name": f"fake://{name}",
        "image": "omavroom-base",
        "agent_label": agent,
        "last_error": None,
        "attempts": 0,
        "lease_expires_at": None,
        "last_heartbeat": None,
        "pending_action": None,
        "needs_attention": False,
    }
    seat.update(extra)
    return seat


def _claimed(request_id, seat_id, agent, *, project=None):
    return {
        "id": request_id,
        "agent_label": agent,
        "seat_type": "terminal",
        "project": project,
        "image": "omavroom-base",
        "status": "claimed",
        "position": 1,
        "seat_id": seat_id,
        "queue_ahead": 0,
        "created_at": "2026-01-01T11:29:00.000000Z",
        "updated_at": "2026-01-01T11:30:00.000000Z",
        "seat": None,
        "lease": {
            "id": request_id,
            "seat_id": seat_id,
            "request_id": request_id,
            "agent_label": agent,
            "acquired_at": "2026-01-01T11:30:00.000000Z",
            "expires_at": "2026-01-01T13:00:00.000000Z",
            "last_heartbeat": "2026-01-01T11:59:15.000000Z",
        },
    }


def _waiting(request_id, agent, *, project=None, position=1, queue_ahead=0):
    return {
        "id": request_id,
        "agent_label": agent,
        "seat_type": "terminal",
        "project": project,
        "image": "omavroom-base",
        "status": "waiting",
        "position": position,
        "seat_id": None,
        "queue_ahead": queue_ahead,
        "created_at": "2026-01-01T11:58:00.000000Z",
        "updated_at": "2026-01-01T11:58:00.000000Z",
        "seat": None,
    }


# --------------------------------------------------------------------------
# render + scene-graph traversal helpers
# --------------------------------------------------------------------------
def _pump(app: QGuiApplication, ms: int = 30) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def _all_items(node) -> list[QObject]:
    """Every visual QQuickItem reachable from ``node`` (scene-graph children)."""
    out: list[QObject] = []

    def walk(item) -> None:
        for child in item.childItems():
            out.append(child)
            walk(child)

    walk(node)
    return out


def _render(app: QGuiApplication, root) -> None:
    """Force a paint pass so Repeater/ListView delegates materialise."""
    QQuickWindow.grabWindow(root)
    _pump(app, 50)


def _find(root, prefix: str) -> list[QObject]:
    base = root.contentItem() if hasattr(root, "contentItem") else root
    return [i for i in _all_items(base) if (i.objectName() or "").startswith(prefix)]


def _texts(item) -> list[str]:
    return [t for t in _deep_texts(item) if t]


def _deep_texts(item) -> list[str]:
    """Every rendered ``text`` under ``item`` (covers ``Label`` and ``Text``).

    QtQuick Controls ``Label`` is not a ``QQuickText`` by class name, so this
    probes the ``text`` property rather than the class.
    """
    found: list[str] = []

    def walk(node) -> None:
        for child in node.childItems():
            prop = child.property("text")
            if isinstance(prop, str) and prop:
                found.append(prop)
            walk(child)

    walk(item)
    return found


def _build_engine(backend: WallBackend) -> QQmlApplicationEngine:
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(QUrl.fromLocalFile(os.path.join(QML_DIR, "Main.qml")))
    app = _qapp()
    for _ in range(5):
        app.processEvents()
    return engine


# --------------------------------------------------------------------------
# FIX 2: the visual layer must be exercised
# --------------------------------------------------------------------------
def test_visual_delegates_render_with_labels_and_off_state():
    app = _qapp()
    cfg = _config(desktop=1, terminal=2)
    backend = WallBackend(cfg)
    status = _status(
        seats=[_seat(1, "terminal-1", agent="alice")],
        per_type={"desktop": {"max_seats": 1}, "terminal": {"max_seats": 2}},
    )
    status["queue"] = [_claimed(7, 1, "alice", project="api")]
    backend.apply_payload({"ok": True, "status": status, "execs": {}, "screenshots": {}})
    assert backend.queueCount == 0  # claimed requests are not waiters

    engine = _build_engine(backend)
    try:
        roots = engine.rootObjects()
        assert roots, "Main.qml failed to produce a root object"
        root = roots[0]
        _render(app, root)

        tiles = _find(root, "slotTile-")
        names = sorted(t.objectName() for t in tiles)
        assert names == ["slotTile-desktop-0", "slotTile-terminal-0", "slotTile-terminal-1"]
        assert root.property("slotCount") == 3

        by_name = {t.objectName(): t for t in tiles}

        # Off / no signal tile (empty desktop slot).
        desktop = by_name["slotTile-desktop-0"]
        assert desktop.property("occupied") is False
        assert desktop.property("desktop") is True
        assert "off / no signal" in _texts(desktop)

        # Live terminal slot: agent/project labels must be rendered.
        terminal = by_name["slotTile-terminal-0"]
        assert terminal.property("occupied") is True
        labels = " || ".join(_texts(terminal))
        assert "agent alice" in labels
        assert "project api" in labels
        assert "terminal" in labels

        # The other terminal slot stays off.
        assert "off / no signal" in _texts(by_name["slotTile-terminal-1"])
    finally:
        engine.deleteLater()
        app.processEvents()


def test_visual_delegates_stay_put_on_teardown():
    app = _qapp()
    backend = WallBackend(_config(desktop=1, terminal=2))
    per_type = {"desktop": {"max_seats": 1}, "terminal": {"max_seats": 2}}
    backend.apply_payload(
        {
            "ok": True,
            "status": _status(seats=[_seat(1, "terminal-1", agent="alice")], per_type=per_type),
            "execs": {},
            "screenshots": {},
        }
    )
    engine = _build_engine(backend)
    try:
        root = engine.rootObjects()[0]
        _render(app, root)
        before = sorted(t.objectName() for t in _find(root, "slotTile-"))
        assert "agent alice" in " || ".join(_texts(_find(root, "slotTile-terminal-0")[0]))

        # The seat exits: the tile stays, only its screen turns off.
        backend.apply_payload(
            {
                "ok": True,
                "status": _status(seats=[], per_type=per_type),
                "execs": {},
                "screenshots": {},
            }
        )
        _render(app, root)
        after = sorted(t.objectName() for t in _find(root, "slotTile-"))
        assert after == before
        terminal = _find(root, "slotTile-terminal-0")[0]
        assert terminal.property("occupied") is False
        assert "off / no signal" in _texts(terminal)
    finally:
        engine.deleteLater()
        app.processEvents()


def test_visual_queue_and_attention_panels_render():
    app = _qapp()
    backend = WallBackend(_config(desktop=0, terminal=1))
    status = _status(
        seats=[
            _seat(
                1,
                "terminal-1",
                state="held",
                agent="dave",
                needs_attention=True,
                last_error="export failed",
            )
        ],
        per_type={"desktop": {"max_seats": 0}, "terminal": {"max_seats": 1}},
    )
    status["queue"] = [_waiting(8, "ned", project="later")]
    backend.apply_payload({"ok": True, "status": status, "execs": {}, "screenshots": {}})

    engine = _build_engine(backend)
    try:
        root = engine.rootObjects()[0]
        _render(app, root)
        queue = _find(root, "queuePanel")
        attention = _find(root, "attentionPanel")
        assert queue, "queue panel delegate did not render"
        assert attention, "attention panel delegate did not render"
        queue_text = " || ".join(_texts(queue[0]))
        assert "ned" in queue_text and "later" in queue_text
        assert "NEXT" in queue_text
        attention_text = " || ".join(_texts(attention[0]))
        assert "dave" in attention_text
        assert "export failed" in attention_text
    finally:
        engine.deleteLater()
        app.processEvents()


# --------------------------------------------------------------------------
# Fit-to-window wall: every slot fits inside the wall area (no scrolling),
# and clicking a monitor focuses/enlarges it.
# --------------------------------------------------------------------------
def _empty_payload():
    return {
        "ok": True,
        "status": _status(
            seats=[], per_type={"desktop": {"max_seats": 1}, "terminal": {"max_seats": 2}}
        ),
        "execs": {},
        "screenshots": {},
    }


def test_wall_fits_all_slots_inside_the_wall_area():
    app = _qapp()
    cfg = _config(desktop=1, terminal=2)
    backend = WallBackend(cfg)
    backend.apply_payload(_empty_payload())
    engine = _build_engine(backend)
    try:
        root = engine.rootObjects()[0]
        _render(app, root)
        wall = root.findChild(QObject, "wallArea")
        assert wall is not None
        width = float(wall.property("width"))
        height = float(wall.property("height"))
        assert width > 0 and height > 0
        rects = [backend.slotRectAt(i) for i in range(len(backend.slotKeys))]
        assert len(rects) == 3
        for rect in rects:
            assert rect["x"] >= -0.5
            assert rect["y"] >= -0.5
            assert rect["x"] + rect["width"] <= width + 0.5
            assert rect["y"] + rect["height"] <= height + 0.5
        # Everything fits AND fills the height: no empty band beneath the wall.
        assert max(r["y"] + r["height"] for r in rects) >= height - 1.0
        # Capture widths are config-driven now, not the old 2x-largest-tile
        # heuristic.
        assert backend.thumbnailWidth == cfg.gui.thumbnail_width
        assert backend.focusedWidth == cfg.gui.focused_width
    finally:
        engine.deleteLater()
        app.processEvents()


def test_toggle_focus_enlarges_the_selected_slot():
    app = _qapp()
    backend = WallBackend(_config(desktop=1, terminal=2))
    backend.apply_payload(_empty_payload())
    engine = _build_engine(backend)
    try:
        root = engine.rootObjects()[0]
        _render(app, root)
        keys = backend.slotKeys
        backend.toggleFocus(keys[0])
        assert backend.focusedSlotKey == keys[0]
        focused = backend.slotRectAt(0)
        other = backend.slotRectAt(1)
        assert focused["focused"] is True
        assert other["focused"] is False
        assert focused["width"] * focused["height"] > other["width"] * other["height"]
        # Clicking the same monitor again restores the uniform wall.
        backend.toggleFocus(keys[0])
        assert backend.focusedSlotKey == ""
    finally:
        engine.deleteLater()
        app.processEvents()


def test_viewport_width_repacks_columns_at_boundaries():
    backend = WallBackend(_config())
    # Below two units: one column. At/above two units: two. Etc.
    backend.setViewportWidth(100)
    assert backend.gridColumns == 1
    backend.setViewportWidth(520)
    assert backend.gridColumns == 2
    backend.setViewportWidth(1100)
    assert backend.gridColumns == 4
    backend.setViewportWidth(100)
    assert backend.gridColumns == 1


# --------------------------------------------------------------------------
# FIX 3: peek gating
# --------------------------------------------------------------------------
def test_terminal_slot_is_not_peekable():
    backend = WallBackend(_config(desktop=1, terminal=1))
    status = _status(
        seats=[
            _seat(1, "desktop-1", seat_type="desktop", agent="gfx"),
            _seat(2, "terminal-1", agent="cli"),
        ],
        per_type={"desktop": {"max_seats": 1}, "terminal": {"max_seats": 1}},
    )
    backend.apply_payload({"ok": True, "status": status, "execs": {}, "screenshots": {}})
    by_key = {slot["key"]: slot for slot in (backend.slotAt(i) for i in range(2))}
    assert by_key["desktop-0"]["peekable"] is True
    assert by_key["terminal-0"]["peekable"] is False


def test_request_peek_terminal_is_rejected_without_emitting():
    backend = WallBackend(_config(desktop=0, terminal=1))
    backend.apply_payload(
        {
            "ok": True,
            "status": _status(
                seats=[_seat(1, "terminal-1", agent="cli")],
                per_type={"desktop": {"max_seats": 0}, "terminal": {"max_seats": 1}},
            ),
            "execs": {},
            "screenshots": {},
        }
    )
    seen: list = []
    backend.requestAction.connect(lambda action, seat_id: seen.append((action, seat_id)))
    backend.requestPeek(1)
    assert seen == []
    assert "desktop seats" in backend.lastMessage


def test_request_peek_desktop_is_forwarded():
    backend = WallBackend(_config(desktop=1, terminal=0))
    backend.apply_payload(
        {
            "ok": True,
            "status": _status(
                seats=[_seat(1, "desktop-1", seat_type="desktop", agent="gfx")],
                per_type={"desktop": {"max_seats": 1}, "terminal": {"max_seats": 0}},
            ),
            "execs": {},
            "screenshots": {},
        }
    )
    seen: list = []
    backend.requestAction.connect(lambda action, seat_id: seen.append((action, seat_id)))
    backend.requestPeek(1)
    assert seen == [("peek", 1)]


def test_visual_terminal_tile_is_not_peekable():
    app = _qapp()
    backend = WallBackend(_config(desktop=1, terminal=1))
    status = _status(
        seats=[
            _seat(1, "desktop-1", seat_type="desktop", agent="gfx"),
            _seat(2, "terminal-1", agent="cli"),
        ],
        per_type={"desktop": {"max_seats": 1}, "terminal": {"max_seats": 1}},
    )
    backend.apply_payload({"ok": True, "status": status, "execs": {}, "screenshots": {}})
    engine = _build_engine(backend)
    try:
        root = engine.rootObjects()[0]
        _render(app, root)
        by_name = {t.objectName(): t for t in _find(root, "slotTile-")}
        assert by_name["slotTile-terminal-0"].property("peekable") is False
        assert by_name["slotTile-desktop-0"].property("peekable") is True
    finally:
        engine.deleteLater()
        app.processEvents()


# --------------------------------------------------------------------------
# backend-only behaviour
# --------------------------------------------------------------------------
def test_backend_exposes_labels_and_terminal_text():
    backend = WallBackend(_config(desktop=1, terminal=1))
    status = _status(
        seats=[
            _seat(1, "desktop-1", seat_type="desktop", agent="gfx"),
            _seat(2, "terminal-1", agent="cli"),
        ],
        per_type={"desktop": {"max_seats": 1}, "terminal": {"max_seats": 1}},
    )
    status["queue"] = [_claimed(7, 2, "cli", project="api"), _waiting(8, "ned", project="later")]
    execs = {
        2: [
            {
                "exec_id": "e",
                "label": "run",
                "state": "running",
                "started_at": "2026-01-01T11:00:00.000000Z",
                "stdout": "hello world",
                "stderr": "",
            }
        ]
    }
    backend.apply_payload(
        {"ok": True, "status": status, "execs": execs, "screenshots": {1: "QUJD"}}
    )
    data = {slot["key"]: slot for slot in (backend.slotAt(i) for i in range(2))}
    assert data["terminal-0"]["agent"] == "cli"
    assert data["terminal-0"]["project"] == "api"
    assert "hello world" in data["terminal-0"]["terminal_text"]
    assert data["desktop-0"]["thumbnail_source"] == "data:image/png;base64,QUJD"
    assert data["desktop-0"]["off_text"] == "off / no signal"
    assert backend.queueCount == 1
    assert backend.queue[0]["agent"] == "ned" and backend.queue[0]["project"] == "later"
    assert backend.queue[0]["next_up"] is True


def test_backend_queue_and_attention_panels():
    backend = WallBackend(_config(desktop=0, terminal=1))
    status = _status(
        seats=[
            _seat(
                1,
                "terminal-1",
                state="held",
                agent="dave",
                needs_attention=True,
                last_error="export failed",
            )
        ],
        per_type={"desktop": {"max_seats": 0}, "terminal": {"max_seats": 1}},
    )
    status["queue"] = [_claimed(7, 1, "dave", project="demo")]
    backend.apply_payload({"ok": True, "status": status, "execs": {}, "screenshots": {}})
    assert backend.attentionCount == 1
    item = backend.attention[0]
    assert item["name"] == "terminal-1"
    assert item["last_error"] == "export failed"
    assert tuple(item["actions"]) == ATTENTION_ACTIONS


def test_backend_daemon_down_flag():
    backend = WallBackend(_config())
    assert backend.daemonOk is False
    backend.apply_payload({"ok": False, "error": "daemon is not running at /missing"})
    assert backend.daemonOk is False
    assert "not running" in backend.daemonMessage
    backend.apply_payload(
        {
            "ok": True,
            "status": _status(
                seats=[], per_type={"desktop": {"max_seats": 1}, "terminal": {"max_seats": 2}}
            ),
            "execs": {},
            "screenshots": {},
        }
    )
    assert backend.daemonOk is True


def test_open_viewer_never_autolaunches_without_command(monkeypatch):
    backend = WallBackend(_config(), viewer_command=None)
    calls: list = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: calls.append(a))
    backend.openViewer("vnc://127.0.0.1:5900")
    assert calls == []
    assert "no viewer configured" in backend.lastMessage


def test_open_viewer_launches_only_when_explicitly_called(monkeypatch):
    backend = WallBackend(_config(), viewer_command="vncviewer {}")
    calls: list = []

    class _Proc:
        pass

    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: (calls.append(argv), _Proc())[1])
    backend.openViewer("vnc://127.0.0.1:5900")
    assert calls == [["vncviewer", "vnc://127.0.0.1:5900"]]


# --------------------------------------------------------------------------
# Settings dialog: golden-image source profile
# --------------------------------------------------------------------------
def test_settings_dialog_exposes_golden_profile_combo():
    app = _qapp()
    backend = WallBackend(_config())
    engine = _build_engine(backend)
    try:
        root = engine.rootObjects()[0]
        dialog = root.findChild(QObject, "settingsDialog")
        assert dialog is not None
        dialog.setProperty("visible", True)
        _render(app, root)

        combos = _find(root, "goldenProfileBox")
        assert combos, "golden profile combo did not render"
        combo = combos[0]
        assert combo.property("count") == 2
        assert backend.goldenProfile == "stock"

        # Selecting a profile forwards the change to the backend/daemon path.
        seen: list = []
        backend.requestConfigValue.connect(
            lambda section, key, value: seen.append((section, key, value))
        )
        backend.setGoldenProfile("mirror")
        assert seen == [("golden", "profile", "mirror")]
        assert backend.goldenProfile == "mirror"

        # An unknown profile is refused and never reaches the daemon.
        backend.setGoldenProfile("bogus")
        assert seen == [("golden", "profile", "mirror")]
        assert "unknown golden profile" in backend.lastMessage
    finally:
        engine.deleteLater()
        app.processEvents()


# --------------------------------------------------------------------------
# Package A: adaptive capture (config-driven, focus-aware)
# --------------------------------------------------------------------------
class _FakeCaptureClient:
    """Minimal daemon client recording screenshot widths for one desktop seat."""

    def __init__(self, seats: list[dict]) -> None:
        self.seats = seats
        self.screenshots: list[tuple[int, int]] = []

    def pool_status(self) -> dict:
        return {"seats": self.seats, "per_type": {"desktop": {"max_seats": 1}}}

    def list_execs(self, seat_id: int) -> list[dict]:
        return []

    def screenshot(self, seat_id: int, *, max_width: int | None = None):
        self.screenshots.append((int(seat_id), int(max_width or 0)))
        return b"PNG"

    def close(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class _FakeMonotonic:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_settings_dialog_exposes_capture_controls():
    app = _qapp()
    cfg = _config()
    backend = WallBackend(cfg)
    engine = _build_engine(backend)
    try:
        root = engine.rootObjects()[0]
        dialog = root.findChild(QObject, "settingsDialog")
        assert dialog is not None
        dialog.setProperty("visible", True)
        _render(app, root)
        assert _find(root, "liveModeBox"), "live-mode combo did not render"
        assert backend.thumbnailWidth == cfg.gui.thumbnail_width
        assert backend.focusedWidth == cfg.gui.focused_width
        assert backend.wallInterval == cfg.gui.wall_interval_s

        seen: list = []
        backend.requestConfigValue.connect(
            lambda section, key, value: seen.append((section, key, value))
        )
        backend.setGuiSetting("live_mode", "vnc")
        backend.setGuiSetting("focused_width", "1600")
        assert ("gui", "live_mode", "vnc") in seen
        assert ("gui", "focused_width", "1600") in seen
        assert backend.liveMode == "vnc"
        # Package C: vnc is now an active mode (no longer a reserved notice).
        assert backend.liveModeNotice == ""
        assert backend.focusedWidth == 1600

        # A bad value is refused and never reaches the daemon.
        before = len(seen)
        backend.setGuiSetting("live_mode", "hologram")
        assert len(seen) == before
        assert "live_mode" in backend.lastMessage
    finally:
        engine.deleteLater()
        app.processEvents()


def test_focus_triggers_high_res_capture_and_clear_returns_to_thumbnail():
    app = _qapp()
    cfg = _config(desktop=1, terminal=0)
    backend = WallBackend(cfg)
    clock = _FakeMonotonic()
    planner = CapturePlanner(
        thumbnail_width=cfg.gui.thumbnail_width,
        focused_width=cfg.gui.focused_width,
        focused_interval_s=cfg.gui.focused_interval_s,
        wall_interval_s=cfg.gui.wall_interval_s,
        clock=clock,
    )
    client = _FakeCaptureClient([_seat(5, "desktop-1", seat_type="desktop", agent="gfx")])
    worker = PollWorker("unused", planner=planner, client=client)
    backend.requestFocus.connect(worker.set_focus)

    backend.apply_payload(
        {
            "ok": True,
            "status": _status(
                seats=[_seat(5, "desktop-1", seat_type="desktop", agent="gfx")],
                per_type={"desktop": {"max_seats": 1}, "terminal": {"max_seats": 0}},
            ),
            "execs": {},
            "screenshots": {},
        }
    )
    worker.poll_once()  # first tick: full wall pass at thumbnail width
    assert client.screenshots == [(5, 480)]

    client.screenshots.clear()
    backend.toggleFocus("desktop-0")  # emits requestFocus -> worker.set_focus
    assert backend.focusedSlotKey == "desktop-0"
    assert client.screenshots == [(5, 1024)]  # prompt high-res capture

    backend.clearFocus()  # emits -1
    client.screenshots.clear()
    clock.advance(2.0)  # next wall pass
    worker.poll_once()
    assert client.screenshots == [(5, 480)]  # tile returns to thumbnail cadence
    app.processEvents()


# --------------------------------------------------------------------------
# FIX 4: the settings dialog applies the whole capture set atomically
# --------------------------------------------------------------------------
def test_apply_capture_settings_combined_raise_is_atomic():
    backend = WallBackend(_config())
    assert (backend.thumbnailWidth, backend.focusedWidth) == (480, 1024)
    seen: list = []
    backend.requestConfigValues.connect(lambda section, values: seen.append((section, values)))

    # Raising thumbnail+focused and focused_interval+wall together must all
    # succeed; per-key writes would reject an intermediate pair.
    backend.applyCaptureSettings(
        {
            "thumbnail_width": 1600,
            "focused_width": 2048,
            "focused_interval_s": 1.5,
            "wall_interval_s": 3.0,
        }
    )
    assert (backend.thumbnailWidth, backend.focusedWidth) == (1600, 2048)
    assert backend.focusedInterval == 1.5
    assert backend.wallInterval == 3.0
    assert seen and seen[-1][0] == "gui"
    assert seen[-1][1]["thumbnail_width"] == 1600
    assert seen[-1][1]["focused_width"] == 2048
    assert seen[-1][1]["wall_interval_s"] == 3.0


def test_apply_capture_settings_rejects_invalid_without_partial_change():
    backend = WallBackend(_config())
    before = (backend.thumbnailWidth, backend.focusedWidth)
    backend.applyCaptureSettings({"thumbnail_width": 1600, "focused_width": 100})
    assert (backend.thumbnailWidth, backend.focusedWidth) == before
    assert "focused_width" in backend.lastMessage
