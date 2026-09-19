"""Thin Qt bridge between the pure wall model and QML (Phase 8).

Two classes:

- :class:`WallBackend` lives on the GUI thread. It owns a
  :class:`~omavroom.gui.viewmodel.MonitorWall`, exposes QML properties/slots,
  and *never* touches the daemon socket itself.
- :class:`PollWorker` lives on a dedicated :class:`QThread`. It owns the
  ``DaemonClient`` and does every blocking daemon call (``pool_status``,
  ``list_execs``, ``screenshot``, recovery jobs). It posts plain dicts back to
  the backend through queued signals, so a slow or hung daemon can never block
  the UI thread.

Shutdown never blocks the GUI thread on a hung daemon: the app calls
``request_stop`` (a cross-thread queued slot that sets a cooperative
interrupt flag and stops the timer on the worker thread), and
:func:`omavroom.gui.app.run_gui` only *caps* the join. A poll checks the flag
between per-seat calls and abandons the remainder of its tick, so even a
daemon that accepts connections but never answers cannot pin teardown.

QML observes the wall through a stable key list plus a revision counter::

    Repeater { model: backend.slotKeys
        delegate: Item { property var d: (backend.revision, backend.slotAt(index)) } }

``slotKeys`` only notifies when the (settings-derived) plan changes, so poll
updates never recreate delegates; ``revision`` re-evaluates each delegate's
``d`` binding in place. ``Main.qml`` also mirrors the model into a non-visual
``Instantiator`` of ``slotProbe-*`` objects; the Qt smoke test additionally
drives a real render pass and asserts on the visual ``SlotTile`` delegates.

Live (VNC) path
---------------
When ``gui.live_mode`` is ``"vnc"`` the focused desktop seat streams through
the backend's :class:`~omavroom.gui.frames.FrameSource` (a
:class:`~omavroom.gui.frames.VncFrameSource`) instead of the base64 still
snapshot. ``WallBackend`` only tracks *which* seat should stream and asks the
worker thread to resolve its endpoint (``requestLiveEndpoint`` -> the existing
``actionResult``); the source runs its own socket thread and the GUI thread
polls its revision counter, exposing ``liveSeatId``/``liveRevision`` so QML can
render ``image://omavroom/<seat_id>?v=<revision>``. Every other tile keeps its
adaptive still, and any stream error falls back to stills with a notice.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from omavroom.client import DaemonClient, DaemonClientError, DaemonRequestError, DaemonTimeout
from omavroom.config import GOLDEN_PROFILES, Config
from omavroom.gui.capture import MAX_WIDTH, MIN_WIDTH, CapturePlanner
from omavroom.gui.frames import FrameSource, VncFrameSource
from omavroom.gui.viewmodel import (
    MonitorWall,
    grid_columns,
    wall_layout,
)
from omavroom.poolview import now_utc

#: Per-request socket timeout for polling calls (keeps a hung daemon from
#: pinning a worker tick for the default 30 s).
POLL_REQUEST_TIMEOUT_S = 8.0
#: How long a recovery job (retry-release / force-discard / destroy) may run.
ACTION_TIMEOUT_S = 300.0
#: How long ``run_gui`` waits on the worker thread during shutdown. The wait
#: is only a join cap: the worker is interrupted cooperatively first, so the
#: app returns within this bound even if a daemon call is still in flight.
SHUTDOWN_WAIT_MS = 1500


def _parse_config_value(text: str) -> object:
    """Interpret a config value as JSON when possible, else as a plain string."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


#: How often the GUI thread polls the live frame source for a new revision.
#: ~30 Hz: cheap (a lock + an int compare) and smooth enough for the wall.
LIVE_POLL_INTERVAL_MS = 33


class WallBackend(QObject):
    """QML-facing view of the wall, queue, attention list and settings."""

    slotKeysChanged = Signal()
    revisionChanged = Signal()
    layoutChanged = Signal()
    statusChanged = Signal()
    queueChanged = Signal()
    attentionChanged = Signal()
    noticeChanged = Signal()
    liveChanged = Signal()
    peekReady = Signal(int, str)
    #: Emitted to ask the worker (another thread) to do something blocking.
    requestPoll = Signal()
    requestAction = Signal(str, int)
    requestAdmission = Signal(str)
    #: Push the effective adaptive-capture settings to the worker thread.
    requestCaptureConfig = Signal(object)
    #: Tell the worker which desktop seat is focused (-1 for none).
    requestFocus = Signal(int)
    #: Resolve the VNC endpoint for a focused desktop seat (worker thread).
    requestLiveEndpoint = Signal(int)
    #: Ask the worker to persist a config value (section, key, value) via the daemon.
    requestConfigValue = Signal(str, str, str)
    #: Persist a whole section's keys atomically (section, values dict).
    requestConfigValues = Signal(str, object)

    def __init__(
        self,
        config: Config,
        *,
        poll_interval_s: float | None = None,
        screenshot_width: int | None = None,
        focused_width: int | None = None,
        focused_interval_s: float | None = None,
        viewer_command: str | None = None,
        frame_source: FrameSource | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self._wall = MonitorWall(config)
        self._revision = 0
        self._columns = grid_columns(0)
        #: Wall area (already excludes the sidebar) for fit-to-window layout.
        self._viewport_w = 0
        self._viewport_h = 0
        #: Key of the enlarged/focused slot, or None for the uniform wall.
        self._focused_key: str | None = None
        #: Last focus seat id pushed to the worker, so we only emit on change.
        self._focused_seat_id_sent: int | None = None
        self._layout_revision = 0
        self._daemon_ok = False
        self._daemon_message = "connecting to daemon..."
        self._notice = ""
        # Effective capture settings: config values, optionally overridden for
        # this session by the CLI (``--interval`` / ``--screenshot-width``).
        gui = config.gui
        self._wall_interval = (
            float(poll_interval_s) if poll_interval_s is not None else float(gui.wall_interval_s)
        )
        self._thumbnail_width = (
            int(screenshot_width) if screenshot_width is not None else int(gui.thumbnail_width)
        )
        self._focused_width = (
            int(focused_width) if focused_width is not None else int(gui.focused_width)
        )
        self._focused_interval = (
            float(focused_interval_s)
            if focused_interval_s is not None
            else float(gui.focused_interval_s)
        )
        self._live_mode = str(gui.live_mode)
        self._live_mode_notice = ""
        #: Live VNC state: the seat currently streaming (-1 none), the focus we
        #: want to stream (or None), and the last frame revision delivered.
        self._frame_source: FrameSource = frame_source or VncFrameSource()
        self._live_seat_id = -1
        self._live_target: int | None = None
        self._live_revision = 0
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(LIVE_POLL_INTERVAL_MS)
        self._live_timer.timeout.connect(self._poll_live)
        self._viewer_command = viewer_command
        self._settings_text = ""
        self._refresh_settings_text()

    # -- model projections for QML --------------------------------------
    @Property("QVariantList", notify=slotKeysChanged)
    def slotKeys(self) -> list[str]:
        return self._wall.state.slot_keys

    @Property(int, notify=revisionChanged)
    def revision(self) -> int:
        return self._revision

    @Slot(int, result="QVariantMap")
    def slotAt(self, index: int) -> dict:
        return self._wall.state.slot_at(index)

    @Property(bool, notify=statusChanged)
    def daemonOk(self) -> bool:
        return self._daemon_ok

    @Property(str, notify=statusChanged)
    def daemonMessage(self) -> str:
        return self._daemon_message

    @Property(str, notify=statusChanged)
    def poolText(self) -> str:
        return self._wall.state.pool_text or "connecting..."

    @Property(str, notify=statusChanged)
    def freeText(self) -> str:
        return self._wall.state.free_text

    @Property(str, notify=statusChanged)
    def headroomText(self) -> str:
        return self._wall.state.headroom_text

    @Property(str, notify=statusChanged)
    def admissionOverride(self) -> str:
        return self._wall.state.admission_override

    @Property(str, notify=statusChanged)
    def goldenProfile(self) -> str:
        """Golden-image source profile (``stock`` or ``mirror``)."""
        return self.config.golden.profile

    @Property(str, notify=statusChanged)
    def settingsText(self) -> str:
        return self._settings_text

    @Property(float, notify=statusChanged)
    def pollInterval(self) -> float:
        """Wall cadence in seconds (the slow pass)."""
        return self._wall_interval

    @Property(int, notify=statusChanged)
    def thumbnailWidth(self) -> int:
        """Wall-scale capture width in pixels."""
        return self._thumbnail_width

    @Property(int, notify=statusChanged)
    def focusedWidth(self) -> int:
        """Focused-monitor capture width in pixels."""
        return self._focused_width

    @Property(float, notify=statusChanged)
    def focusedInterval(self) -> float:
        """Fast cadence for the focused monitor, in seconds."""
        return self._focused_interval

    @Property(float, notify=statusChanged)
    def wallInterval(self) -> float:
        """Slow wall cadence in seconds."""
        return self._wall_interval

    @Property(str, notify=statusChanged)
    def liveMode(self) -> str:
        """Capture mode: ``stills`` (default) or ``vnc`` (focused monitor)."""
        return self._live_mode

    @Property(int, notify=liveChanged)
    def liveSeatId(self) -> int:
        """The seat currently streaming over VNC, or ``-1`` for none."""
        return self._live_seat_id

    @Property(int, notify=liveChanged)
    def liveRevision(self) -> int:
        """Monotonic counter bumped per delivered live frame (cache buster)."""
        return self._live_revision

    @Property(str, notify=noticeChanged)
    def liveModeNotice(self) -> str:
        """Live-mode status/notice; non-empty only when VNC fell back."""
        return self._live_mode_notice

    @property
    def frame_source(self) -> FrameSource:
        """The frame source behind the QML image provider (read-only)."""
        return self._frame_source

    @Property(str, notify=noticeChanged)
    def lastMessage(self) -> str:
        return self._notice

    @Property(int, notify=layoutChanged)
    def gridColumns(self) -> int:
        return self._columns

    @Property(str, notify=layoutChanged)
    def focusedSlotKey(self) -> str:
        """Key of the focused (enlarged) slot, or "" for the uniform wall."""
        return self._focused_key or ""

    @Property(int, notify=layoutChanged)
    def layoutRevision(self) -> int:
        """Bumped whenever viewport size, focus, or the slot plan changes.

        QML uses it as a dependency so ``slotRectAt`` re-evaluates without the
        delegate being recreated.
        """
        return self._layout_revision

    @Property("QVariantList", notify=queueChanged)
    def queue(self) -> list[dict]:
        return self._wall.state.waiter_dicts()

    @Property(int, notify=queueChanged)
    def queueCount(self) -> int:
        return len(self._wall.state.waiters)

    @Property("QVariantList", notify=attentionChanged)
    def attention(self) -> list[dict]:
        return self._wall.state.attention_dicts()

    @Property(int, notify=attentionChanged)
    def attentionCount(self) -> int:
        return len(self._wall.state.attention)

    # -- update path (called on the GUI thread) --------------------------
    @Slot(object)
    def apply_payload(self, payload: object) -> None:
        """Fold one worker snapshot (``ok`` + status/execs/screenshots) in."""
        if not isinstance(payload, dict):
            return
        if not payload.get("ok"):
            self._daemon_ok = False
            self._daemon_message = str(payload.get("error") or "daemon not running")
            self.statusChanged.emit()
            return
        status = payload.get("status") or {}
        plan_changed = self._wall.sync_plan(status.get("per_type") or {})
        # A focus on a slot that no longer exists would be a stale rectangle.
        if self._focused_key and self._focused_key not in self._wall.state.slot_keys:
            self._focused_key = None
            plan_changed = True
        self._wall.update(
            status,
            execs_by_seat=payload.get("execs") or {},
            screenshots=payload.get("screenshots") or {},
            now=now_utc(),
        )
        self._daemon_ok = True
        self._daemon_message = ""
        self._revision += 1
        if plan_changed:
            self.slotKeysChanged.emit()
            self._bump_layout()
        # A seat swap or teardown can change *which* seat is focused even when
        # the focused key survives; keep the worker's focus in step.
        self._sync_focus()
        self.revisionChanged.emit()
        self.queueChanged.emit()
        self.attentionChanged.emit()
        self.statusChanged.emit()

    @Slot(str, bool, str, int, str)
    def on_action_result(
        self, action: str, ok: bool, message: str, seat_id: int, endpoint: str
    ) -> None:
        if action == "live-endpoint":
            self._on_live_endpoint(seat_id, ok, message, endpoint)
            return
        self._set_notice(f"{action}: {message}" if message else action)
        if action == "peek" and ok and endpoint:
            self.peekReady.emit(seat_id, endpoint)

    # -- operator actions (emit cross-thread requests) -------------------
    @Slot()
    def retryDaemon(self) -> None:
        self.requestPoll.emit()

    @Slot(int)
    def requestPeek(self, seat_id: int) -> None:
        """Request a peek endpoint, but only for a seat type that has one.

        Click-to-peek is gated here as well as in QML, so a stale/malicious
        call cannot dead-end on a terminal seat. The peer check looks the seat
        up in the current wall state.
        """
        if not self._is_peekable_seat(seat_id):
            self._set_notice("peek is only available for desktop seats")
            return
        self.requestAction.emit("peek", int(seat_id))

    def _is_peekable_seat(self, seat_id: int) -> bool:
        for slot in self._wall.state.slots:
            if slot.seat_id == seat_id:
                return slot.peekable
        return False

    @Slot(str, int)
    def requestRecovery(self, action: str, seat_id: int) -> None:
        self.requestAction.emit(str(action), int(seat_id))

    @Slot(str)
    def setAdmission(self, override: str) -> None:
        self.requestAdmission.emit(str(override))

    @Slot(str)
    def setGoldenProfile(self, profile: str) -> None:
        """Apply a golden-image source profile and persist it via the daemon.

        The local config is updated optimistically so the dialog reflects the
        choice immediately; the worker performs the validated daemon write.
        """
        profile = str(profile)
        if profile not in GOLDEN_PROFILES:
            self._set_notice(f"unknown golden profile {profile!r}")
            return
        self.config.set_value("golden", "profile", profile)
        self._refresh_settings_text()
        self.statusChanged.emit()
        self.requestConfigValue.emit("golden", "profile", profile)

    @Slot("QVariantMap")
    def applyCaptureSettings(self, values: object) -> None:
        """Apply the whole ``[gui]`` capture set atomically.

        The settings dialog submits every field at once, so a valid combined
        change (for example raising ``thumbnail_width`` and ``focused_width``
        together, or ``focused_interval_s`` above the old ``wall_interval_s``)
        is validated and applied as one unit instead of being rejected
        mid-sequence. The local config is updated optimistically; the worker
        persists the same set through the daemon in a single write.
        """
        if not isinstance(values, dict) or not values:
            return
        parsed = {str(key): _parse_config_value(value) for key, value in values.items()}
        try:
            coerced = self.config.set_values("gui", parsed)
        except ValueError as exc:
            self._set_notice(str(exc))
            return
        self._thumbnail_width = int(self.config.gui.thumbnail_width)
        self._focused_width = int(self.config.gui.focused_width)
        self._focused_interval = float(self.config.gui.focused_interval_s)
        self._wall_interval = float(self.config.gui.wall_interval_s)
        self._live_mode = str(self.config.gui.live_mode)
        self._live_mode_notice = ""
        self._refresh_settings_text()
        self._emit_capture_config()
        self._sync_live()
        self.statusChanged.emit()
        self.requestConfigValues.emit("gui", coerced)

    @Slot(str, str)
    def setGuiSetting(self, key: str, value: str) -> None:
        """Validate, apply and persist one ``[gui]`` setting.

        The local config is updated optimistically so the dialog and the worker
        reflect the choice immediately; the worker performs the validated
        daemon write. A bad value (or a cross-field conflict such as
        ``wall_interval_s < focused_interval_s``) is refused with a notice.
        """
        key = str(key)
        try:
            coerced = self.config.validate_value("gui", key, _parse_config_value(value))
            self.config.set_value("gui", key, coerced)
        except ValueError as exc:
            self._set_notice(str(exc))
            return
        if key == "thumbnail_width":
            self._thumbnail_width = int(coerced)
        elif key == "focused_width":
            self._focused_width = int(coerced)
        elif key == "focused_interval_s":
            self._focused_interval = float(coerced)
        elif key == "wall_interval_s":
            self._wall_interval = float(coerced)
        elif key == "live_mode":
            self._live_mode = str(coerced)
            self._live_mode_notice = ""
        self._refresh_settings_text()
        self._emit_capture_config()
        self._sync_live()
        self.statusChanged.emit()
        self.requestConfigValue.emit("gui", key, str(coerced))

    @Slot(int)
    def setViewportWidth(self, width: int) -> None:
        """Set the wall's available width (kept for callers/tests).

        Prefer :meth:`setViewportSize`, which also drives the vertical fit.
        """
        columns = grid_columns(width)
        if columns != self._columns:
            self._columns = columns
            self.layoutChanged.emit()

    @Slot(int, int)
    def setViewportSize(self, width: int, height: int) -> None:
        """Set the wall area (sidebar/header already excluded).

        The whole slot set is laid out to fit this area exactly: tiles scale
        down rather than scrolling.
        """
        w = max(0, int(width))
        h = max(0, int(height))
        if (w, h) == (self._viewport_w, self._viewport_h):
            return
        self._viewport_w = w
        self._viewport_h = h
        self._columns = grid_columns(w)
        self._bump_layout()

    @Slot(int, result="QVariantMap")
    def slotRectAt(self, index: int) -> dict:
        """Geometry for slot ``index`` in the current fit-to-window layout."""
        rects = self._slot_rects()
        if 0 <= index < len(rects):
            rect = rects[index]
            return {
                "x": rect.x,
                "y": rect.y,
                "width": rect.width,
                "height": rect.height,
                "focused": rect.focused,
            }
        return {}

    @Slot(str)
    def toggleFocus(self, key: str) -> None:
        """Enlarge ``key`` and shrink the rest; clicking it again restores."""
        key = str(key)
        self._focused_key = None if key == self._focused_key else key
        self._bump_layout()
        self._sync_focus()

    @Slot()
    def clearFocus(self) -> None:
        if self._focused_key is not None:
            self._focused_key = None
            self._bump_layout()
            self._sync_focus()

    def _current_focused_seat_id(self) -> int | None:
        if not self._focused_key:
            return None
        for slot in self._wall.state.slots:
            if slot.key == self._focused_key and slot.seat_id is not None:
                return int(slot.seat_id)
        return None

    def _sync_focus(self) -> None:
        """Push the focused seat id to the worker when it changes."""
        seat_id = self._current_focused_seat_id()
        if seat_id != self._focused_seat_id_sent:
            self._focused_seat_id_sent = seat_id
            self.requestFocus.emit(-1 if seat_id is None else seat_id)
        self._sync_live()

    # -- live VNC path ---------------------------------------------------
    def _live_desired_seat(self) -> int | None:
        """The seat that should be streaming, or ``None``.

        Live VNC is opted into with ``live_mode="vnc"`` and applies only to an
        occupied desktop seat (terminal seats have no display).
        """
        if self._live_mode != "vnc":
            return None
        seat_id = self._current_focused_seat_id()
        if seat_id is None:
            return None
        for slot in self._wall.state.slots:
            if slot.seat_id == seat_id:
                return seat_id if slot.occupied and slot.seat_type == "desktop" else None
        return None

    def _sync_live(self) -> None:
        """Start/stop the focused seat's stream as focus or ``live_mode`` changes.

        The endpoint is resolved by the worker thread (it owns the daemon
        client); this method only manages the desired target and the source.
        """
        desired = self._live_desired_seat()
        if desired == self._live_target:
            return
        if self._live_target is not None:
            self._frame_source.stop(self._live_target)
        self._live_target = desired
        if self._live_seat_id != -1:
            self._live_seat_id = -1
            self.liveChanged.emit()
        if desired is None:
            self._live_timer.stop()
            return
        self.requestLiveEndpoint.emit(desired)

    def _on_live_endpoint(self, seat_id: int, ok: bool, message: str, endpoint: str) -> None:
        """Worker resolved (or failed to resolve) a live endpoint."""
        if seat_id != self._live_target:
            return  # focus moved while the endpoint was in flight
        if not ok or not endpoint:
            self._live_target = None
            self._live_timer.stop()
            self._live_fallback(seat_id, message or "no endpoint")
            return
        try:
            self._frame_source.start(seat_id, self._focused_width, endpoint)
        except Exception as exc:  # noqa: BLE001 - never let a stream kill the GUI
            self._live_target = None
            self._live_timer.stop()
            self._live_fallback(seat_id, str(exc))
            return
        self._live_seat_id = int(seat_id)
        self._live_revision = 0
        self._live_mode_notice = ""
        self._live_timer.start()
        self.liveChanged.emit()
        self.noticeChanged.emit()

    def _live_fallback(self, seat_id: int, reason: str) -> None:
        """Abandon live VNC for ``seat_id`` and tell the operator why."""
        if self._live_seat_id != -1:
            self._live_seat_id = -1
            self.liveChanged.emit()
        self._live_mode_notice = f"live VNC unavailable for seat {seat_id}: {reason}; using stills"
        self._set_notice(self._live_mode_notice)

    @Slot()
    def _poll_live(self) -> None:
        """GUI-thread timer: publish a frame revision when a new one arrives."""
        seat_id = self._live_seat_id
        if seat_id < 0:
            return
        error = self._frame_source.error(seat_id)
        if error:
            self._frame_source.stop(seat_id)
            self._live_target = None
            self._live_timer.stop()
            self._live_fallback(seat_id, error)
            return
        revision = self._frame_source.revision(seat_id)
        if revision != self._live_revision:
            self._live_revision = revision
            self.liveChanged.emit()

    @Slot()
    def shutdown_live(self) -> None:
        """Stop the live timer and every stream (called on app shutdown)."""
        self._live_timer.stop()
        stop_all = getattr(self._frame_source, "stop_all", None)
        if callable(stop_all):
            stop_all()
        self._live_seat_id = -1
        self._live_target = None

    def _slot_rects(self):
        slots = self._wall.state.slots
        seat_types = [slot.seat_type for slot in slots]
        focus_index = None
        if self._focused_key:
            for index, slot in enumerate(slots):
                if slot.key == self._focused_key:
                    focus_index = index
                    break
        return wall_layout(
            seat_types,
            self._viewport_w,
            self._viewport_h,
            focus_index=focus_index,
        )

    def _bump_layout(self) -> None:
        self._layout_revision += 1
        self.layoutChanged.emit()

    def _effective_capture_config(self) -> dict:
        """The session-effective capture settings (config + any CLI override)."""
        return {
            "thumbnail_width": self._thumbnail_width,
            "focused_width": self._focused_width,
            "focused_interval_s": self._focused_interval,
            "wall_interval_s": self._wall_interval,
            "live_mode": self._live_mode,
        }

    def _emit_capture_config(self) -> None:
        self.requestCaptureConfig.emit(self._effective_capture_config())

    @Slot(str)
    def openViewer(self, endpoint: str) -> None:
        """Explicit user action only: launch the configured viewer command.

        Nothing here auto-launches a window; it runs only when QML calls it
        from a button press.
        """
        command = (self._viewer_command or "").strip()
        if not command:
            self._set_notice("no viewer configured (set OMAVROOM_VIEWER or pass --viewer)")
            return
        if not endpoint:
            self._set_notice("no peek endpoint available yet")
            return
        parts = shlex.split(command)
        if not parts:
            self._set_notice("viewer command is empty")
            return
        if any("{}" in part for part in parts):
            parts = [part.replace("{}", endpoint) for part in parts]
        else:
            parts.append(endpoint)
        try:
            subprocess.Popen(parts, start_new_session=True)
        except OSError as exc:
            self._set_notice(f"cannot launch viewer {parts[0]!r}: {exc}")
            return
        self._set_notice(f"launched viewer: {parts[0]}")

    # -- internals -------------------------------------------------------
    def _set_notice(self, message: str) -> None:
        self._notice = message
        self.noticeChanged.emit()

    def _refresh_settings_text(self) -> None:
        from omavroom.cli.format import settings_report

        try:
            body = settings_report(self.config)
        except Exception as exc:  # pragma: no cover - defensive
            body = f"(cannot render settings: {exc})"
        self._settings_text = (
            f"{body}\n"
            f"effective: thumbnail_width={self._thumbnail_width}"
            f" focused_width={self._focused_width}"
            f" focused_interval_s={self._focused_interval:g}"
            f" wall_interval_s={self._wall_interval:g}"
            f" live_mode={self._live_mode}"
        )


class PollWorker(QObject):
    """Blocking daemon I/O on its own thread; posts plain dicts to the GUI."""

    snapshotReady = Signal(object)
    actionResult = Signal(str, bool, str, int, str)

    def __init__(
        self,
        socket_path: str | Path | None,
        *,
        poll_interval_s: float = 2.0,
        screenshot_max_width: int = 480,
        focused_width: int = 1024,
        focused_interval_s: float = 0.5,
        live_mode: str = "stills",
        planner: CapturePlanner | None = None,
        client: DaemonClient | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._socket_path = socket_path
        #: The pure scheduler decides which seats to capture on each tick.
        self._planner = planner or CapturePlanner(
            thumbnail_width=screenshot_max_width,
            focused_width=focused_width,
            focused_interval_s=focused_interval_s,
            wall_interval_s=poll_interval_s,
        )
        self._live_mode = str(live_mode)
        self._client: DaemonClient | None = client
        self._timer: QTimer | None = None
        #: Re-entrancy guard: a slow capture must coalesce, not stack ticks.
        self._polling = False
        #: Thread-safe interrupt. Set directly from the GUI thread at
        #: shutdown; read between per-seat calls so a long tick abandons
        #: early. Deliberately *not* a Qt slot invocation: a queued slot
        #: cannot run while ``poll_once`` is blocked inside a socket read.
        self._stop = threading.Event()
        self._client_lock = threading.Lock()
        #: Last successful status/seat/exec snapshot, reused by fast ticks that
        #: only capture the focused seat (status is refreshed on the wall
        #: cadence). ``None`` forces the next tick to be a full wall pass.
        self._status: dict | None = None
        self._seats: list[dict] = []
        self._execs: dict[int, list[dict]] = {}

    @Slot()
    def start(self) -> None:
        self._stop.clear()
        self._timer = QTimer(self)
        self._timer.setInterval(max(50, int(self._planner.focused_interval_s * 1000)))
        self._timer.timeout.connect(self.poll_once)
        self._timer.start()
        self.poll_once()

    def interrupt(self) -> None:
        """Non-blocking, thread-safe stop request callable from any thread.

        Sets the interrupt flag and closes the client socket, which unblocks
        a ``readline`` waiting on a hung daemon. Does **not** touch the Qt
        timer (that belongs to the worker thread and is stopped by
        :meth:`request_stop` / :meth:`start`); use :meth:`shutdown` when the
        worker's event loop is live.
        """
        self._stop.set()
        with self._client_lock:
            client = self._client
        if client is not None:
            try:
                # ``shutdown`` force-closes without taking the request lock,
                # so this returns immediately even while a call is blocked.
                client.shutdown()
            except Exception:  # pragma: no cover - closing must never raise
                pass

    @Slot()
    def request_stop(self) -> None:
        """Stop from *within* the worker thread: flag + stop the timer.

        Only safe to call via the worker's event loop (it touches the timer,
        which is owned by this thread). For cross-thread shutdown prefer
        :meth:`interrupt`, which cannot block and does not touch the timer.
        """
        self._stop.set()
        if self._timer is not None:
            self._timer.stop()

    @Slot()
    def stop(self) -> None:
        """Worker-thread slot: flag, stop the timer, close the client."""
        self.request_stop()
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover
                pass

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @Slot(int)
    def set_focus(self, seat_id: int) -> None:
        """Point the high-resolution capture at one seat (``-1`` clears).

        Polls immediately when a seat is focused so the enlarged monitor gets a
        crisp frame right away rather than on the next scheduled tick.
        """
        value = None if int(seat_id) < 0 else int(seat_id)
        self._planner.set_focus(value)
        if value is not None:
            self.poll_once()

    @Slot(int)
    def resolve_live_endpoint(self, seat_id: int) -> None:
        """Resolve a focused seat's VNC endpoint for the live (VNC) path.

        Runs on the worker thread where the daemon client lives, then reports
        the endpoint over the existing ``actionResult`` signal so the GUI
        thread can start the stream without touching the daemon socket.
        """
        if self._stop.is_set():
            return
        try:
            client = self._ensure_client()
            endpoint = client.peek_endpoint(int(seat_id))
            self.actionResult.emit(
                "live-endpoint", True, "endpoint resolved", int(seat_id), endpoint
            )
        except DaemonClientError as exc:
            self._close_client()
            self.actionResult.emit("live-endpoint", False, str(exc), int(seat_id), "")

    @Slot(object)
    def set_capture_config(self, cfg: object) -> None:
        """Apply an effective capture-config map (widths/cadence/live mode)."""
        if not isinstance(cfg, dict):
            return
        planner = self._planner
        if "thumbnail_width" in cfg:
            planner.thumbnail_width = max(MIN_WIDTH, min(MAX_WIDTH, int(cfg["thumbnail_width"])))
        if "focused_width" in cfg:
            planner.focused_width = max(MIN_WIDTH, min(MAX_WIDTH, int(cfg["focused_width"])))
        # The focused monitor is the high-resolution view; whichever width just
        # changed, it may never end up below the wall thumbnail.
        if planner.focused_width < planner.thumbnail_width:
            planner.focused_width = planner.thumbnail_width
        if "focused_interval_s" in cfg:
            planner.focused_interval_s = max(0.05, float(cfg["focused_interval_s"]))
        if "wall_interval_s" in cfg:
            planner.wall_interval_s = max(planner.focused_interval_s, float(cfg["wall_interval_s"]))
        if "live_mode" in cfg:
            self._set_live_mode(str(cfg["live_mode"]))
        if self._timer is not None:
            interval_ms = max(50, int(planner.focused_interval_s * 1000))
            if self._timer.interval() != interval_ms:
                self._timer.setInterval(interval_ms)

    def _set_live_mode(self, mode: str) -> None:
        """Record the capture mode; the wall's live source handles ``vnc``."""
        if mode == self._live_mode:
            return
        self._live_mode = mode

    @Slot()
    def poll_once(self) -> None:
        """One worker tick, driven by the QTimer.

        The fast tick only captures the focused seat's screenshot at
        ``focused_interval_s``; ``pool_status``/``list_execs`` (and daemon-down
        detection) run on the slower ``wall_interval_s`` cadence. A tick whose
        capture raises must never escape into the Qt event loop, so every
        failure is reported as a non-fatal snapshot and the timer keeps ticking.
        """
        if self._stop.is_set() or self._polling:
            return
        self._polling = True
        try:
            now = self._planner.now()
            if self._status is None or self._planner.wall_due(now):
                self._wall_tick()
            else:
                self._fast_tick()
        except DaemonClientError as exc:
            self._close_client()
            self._status = None
            self.snapshotReady.emit({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - must never kill the timer/GUI
            print(f"omavroom-gui: capture error: {exc}", file=sys.stderr)
            self.snapshotReady.emit({"ok": False, "error": f"capture error: {exc}"})
        finally:
            self._polling = False

    def _wall_tick(self) -> None:
        """Slow pass: refresh status/execs *and* capture the whole wall."""
        client = self._ensure_client()
        status = client.pool_status()
        if self._stop.is_set():
            return
        seats = status.get("seats") if isinstance(status, dict) else None
        seat_list = (
            [seat for seat in seats if isinstance(seat, dict)] if isinstance(seats, list) else []
        )
        execs: dict[int, list[dict]] = {}
        for seat in seat_list:
            if self._stop.is_set():
                return
            if str(seat.get("seat_type") or "") != "terminal":
                continue
            if str(seat.get("state") or "") not in ("ready", "busy"):
                continue
            try:
                numeric_id = int(seat.get("id"))
            except (TypeError, ValueError):
                continue
            execs[numeric_id] = self._list_execs(client, numeric_id)
        plan = self._planner.plan(self._planner.now(), seat_list)
        screenshots = self._capture_plan(client, plan.captures)
        if self._stop.is_set():
            return
        # Cache only after a fully successful pass, so a partial failure forces
        # the next tick to retry rather than showing a half-stale wall.
        self._status = status if isinstance(status, dict) else {}
        self._seats = seat_list
        self._execs = execs
        self.snapshotReady.emit(
            {"ok": True, "status": self._status, "execs": execs, "screenshots": screenshots}
        )

    def _fast_tick(self) -> None:
        """Fast pass: capture only the focused monitor; reuse the last status."""
        client = self._ensure_client()
        plan = self._planner.plan(self._planner.now(), self._seats)
        screenshots = self._capture_plan(client, plan.captures)
        if self._stop.is_set():
            return
        self.snapshotReady.emit(
            {
                "ok": True,
                "status": self._status or {},
                "execs": self._execs,
                "screenshots": screenshots,
            }
        )

    def _capture_plan(self, client: DaemonClient, captures) -> dict[int, str]:
        screenshots: dict[int, str] = {}
        for capture in captures:
            if self._stop.is_set():
                return screenshots
            shot = self._screenshot(client, capture.seat_id, capture.width)
            if shot is not None:
                screenshots[capture.seat_id] = shot
        return screenshots

    def _await_job(self, job) -> None:
        """Wait for a recovery job, polling the interrupt flag as we go.

        ``JobHandle.wait`` blocks in small intervals anyway, so this uses the
        same bounded polling loop the client exposes but checks
        ``_stop_requested`` between polls and returns as soon as a stop is
        requested (the daemon continues the job regardless; the GUI does not
        wait for it). Raises whatever ``JobHandle.result`` would.
        """
        import time

        deadline = time.monotonic() + ACTION_TIMEOUT_S
        while True:
            if self._stop.is_set():
                return
            view = job.poll()
            if view.get("state") != "pending":
                if view.get("state") == "error":
                    error = view.get("error") or {}
                    raise DaemonRequestError(
                        error.get("code", "job_error"), error.get("message", "job failed")
                    )
                return
            if time.monotonic() >= deadline:
                raise DaemonTimeout(f"job {job.job_id} did not finish within {ACTION_TIMEOUT_S}s")
            time.sleep(0.1)

    @Slot(str, int)
    def perform_action(self, action: str, seat_id: int) -> None:
        if self._stop.is_set():
            return
        try:
            client = self._ensure_client()
            if action == "peek":
                endpoint = client.peek_endpoint(seat_id)
                self.actionResult.emit(action, True, "endpoint resolved", seat_id, endpoint)
            elif action == "retry-release":
                self._await_job(client.retry_release(seat_id))
                self.actionResult.emit(action, True, "completed", seat_id, "")
            elif action == "force-discard":
                self._await_job(client.force_discard(seat_id, reason="force_discard"))
                self.actionResult.emit(action, True, "completed", seat_id, "")
            elif action == "destroy":
                self._await_job(client.force_discard(seat_id, reason="destroy"))
                self.actionResult.emit(action, True, "completed", seat_id, "")
            else:
                self.actionResult.emit(action, False, f"unknown action {action!r}", seat_id, "")
        except DaemonClientError as exc:
            self._close_client()
            self.actionResult.emit(action, False, str(exc), seat_id, "")
        finally:
            self.poll_once()

    @Slot(str)
    def set_admission(self, override: str) -> None:
        if self._stop.is_set():
            return
        try:
            client = self._ensure_client()
            client.set_admission_override(override)
            self.actionResult.emit("admission", True, f"set to {override}", 0, "")
        except DaemonClientError as exc:
            self._close_client()
            self.actionResult.emit("admission", False, str(exc), 0, "")
        finally:
            self.poll_once()

    @Slot(str, str, str)
    def set_config_value(self, section: str, key: str, value: str) -> None:
        """Persist a config value through the daemon (validated server-side)."""
        if self._stop.is_set():
            return
        try:
            client = self._ensure_client()
            result = client.set_config_value(section, key, _parse_config_value(value))
            applied = result.get("value", value) if isinstance(result, dict) else value
            self.actionResult.emit("config", True, f"{section}.{key} = {applied}", 0, "")
        except DaemonClientError as exc:
            self._close_client()
            self.actionResult.emit("config", False, str(exc), 0, "")
        finally:
            self.poll_once()

    @Slot(str, object)
    def set_config_values(self, section: str, values: object) -> None:
        """Persist a whole section's keys in one daemon write (atomic).

        The settings dialog submits all ``[gui]`` capture fields together so a
        valid cross-field change survives the round trip as a unit.
        """
        if self._stop.is_set() or not isinstance(values, dict):
            return
        try:
            client = self._ensure_client()
            result = client.set_config_values(section, values)
            applied = result.get("values", values) if isinstance(result, dict) else values
            self.actionResult.emit("config", True, f"{section} = {applied}", 0, "")
        except DaemonClientError as exc:
            self._close_client()
            self.actionResult.emit("config", False, str(exc), 0, "")
        finally:
            self.poll_once()

    # -- internals -------------------------------------------------------
    def _ensure_client(self) -> DaemonClient:
        with self._client_lock:
            if self._client is None:
                self._client = DaemonClient(
                    self._socket_path,
                    timeout=POLL_REQUEST_TIMEOUT_S,
                    connect_retries=0,
                )
            return self._client

    def _close_client(self) -> None:
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            client.close()

    def _list_execs(self, client: DaemonClient, seat_id: int) -> list[dict]:
        try:
            return list(client.list_execs(seat_id))
        except DaemonClientError:
            return []

    def _screenshot(self, client: DaemonClient, seat_id: int, width: int) -> str | None:
        """Return one base64 frame, or ``None`` to skip this seat this tick.

        Deliberately catches **every** exception (not just ``DaemonClientError``)
        and logs it: this runs on a QTimer slot, where an escaping exception can
        tear down the whole GUI process. A bad frame is non-fatal.
        """
        import base64

        try:
            data = client.screenshot(seat_id, max_width=width)
            if not data:
                return None
            return base64.b64encode(data).decode("ascii")
        except Exception as exc:  # noqa: BLE001 - a capture must never kill the GUI
            print(f"omavroom-gui: screenshot seat {seat_id} failed: {exc}", file=sys.stderr)
            return None


__all__ = [
    "ACTION_TIMEOUT_S",
    "LIVE_POLL_INTERVAL_MS",
    "POLL_REQUEST_TIMEOUT_S",
    "PollWorker",
    "WallBackend",
]
