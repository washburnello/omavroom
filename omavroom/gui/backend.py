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
"""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from omavroom.client import DaemonClient, DaemonClientError, DaemonRequestError, DaemonTimeout
from omavroom.config import GOLDEN_PROFILES, Config
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


class WallBackend(QObject):
    """QML-facing view of the wall, queue, attention list and settings."""

    slotKeysChanged = Signal()
    revisionChanged = Signal()
    layoutChanged = Signal()
    statusChanged = Signal()
    queueChanged = Signal()
    attentionChanged = Signal()
    noticeChanged = Signal()
    peekReady = Signal(int, str)
    #: Emitted to ask the worker (another thread) to do something blocking.
    requestPoll = Signal()
    requestAction = Signal(str, int)
    requestAdmission = Signal(str)
    requestPollInterval = Signal(float)
    #: Ask the worker to change the screenshot capture width (sharp tiles).
    requestScreenshotWidth = Signal(int)
    #: Ask the worker to persist a config value (section, key, value) via the daemon.
    requestConfigValue = Signal(str, str, str)

    def __init__(
        self,
        config: Config,
        *,
        poll_interval_s: float = 2.0,
        viewer_command: str | None = None,
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
        self._layout_revision = 0
        self._screenshot_request_width = 0
        self._daemon_ok = False
        self._daemon_message = "connecting to daemon..."
        self._notice = ""
        self._poll_interval = float(poll_interval_s)
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
        return self._poll_interval

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
            self._emit_screenshot_width()
        self.revisionChanged.emit()
        self.queueChanged.emit()
        self.attentionChanged.emit()
        self.statusChanged.emit()

    @Slot(str, bool, str, int, str)
    def on_action_result(
        self, action: str, ok: bool, message: str, seat_id: int, endpoint: str
    ) -> None:
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

    @Slot(float)
    def setPollInterval(self, seconds: float) -> None:
        value = max(0.25, float(seconds))
        self._poll_interval = value
        self._refresh_settings_text()
        self.requestPollInterval.emit(value)
        self.statusChanged.emit()

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
        self._emit_screenshot_width()

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

    @Slot()
    def clearFocus(self) -> None:
        if self._focused_key is not None:
            self._focused_key = None
            self._bump_layout()

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

    def _emit_screenshot_width(self) -> None:
        """Ask the worker for screenshots as wide as the largest tile.

        Capturing at the display size (2x for crispness, hard-capped) keeps
        the monitor sharp instead of upscaling a small thumbnail.
        """
        rects = self._slot_rects()
        if not rects:
            return
        widest = max(rect.width for rect in rects)
        desired = int(max(640, min(2048, widest * 2)))
        if desired != self._screenshot_request_width:
            self._screenshot_request_width = desired
            self.requestScreenshotWidth.emit(desired)

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
        self._settings_text = f"{body}\nscreenshot_poll_interval_s={self._poll_interval:g}"


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
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._socket_path = socket_path
        self._interval_s = max(0.25, float(poll_interval_s))
        self._screenshot_max_width = int(screenshot_max_width)
        self._client: DaemonClient | None = None
        self._timer: QTimer | None = None
        #: Thread-safe interrupt. Set directly from the GUI thread at
        #: shutdown; read between per-seat calls so a long tick abandons
        #: early. Deliberately *not* a Qt slot invocation: a queued slot
        #: cannot run while ``poll_once`` is blocked inside a socket read.
        self._stop = threading.Event()
        self._client_lock = threading.Lock()

    @Slot()
    def start(self) -> None:
        self._stop.clear()
        self._timer = QTimer(self)
        self._timer.setInterval(int(self._interval_s * 1000))
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

    @Slot(float)
    def set_interval(self, seconds: float) -> None:
        self._interval_s = max(0.25, float(seconds))
        if self._timer is not None:
            self._timer.setInterval(int(self._interval_s * 1000))

    @Slot(int)
    def set_screenshot_width(self, max_width: int) -> None:
        """Capture width for desktop thumbnails (kept sharp to the tile)."""
        self._screenshot_max_width = max(160, min(2048, int(max_width)))

    @Slot()
    def poll_once(self) -> None:
        if self._stop.is_set():
            return
        try:
            client = self._ensure_client()
            status = client.pool_status()
        except DaemonClientError as exc:
            self._close_client()
            self.snapshotReady.emit({"ok": False, "error": str(exc)})
            return
        if self._stop.is_set():
            return
        execs: dict[int, list[dict]] = {}
        screenshots: dict[int, str] = {}
        seats = status.get("seats") if isinstance(status, dict) else None
        for seat in seats if isinstance(seats, list) else []:
            if self._stop.is_set():
                return
            if not isinstance(seat, dict):
                continue
            seat_id = seat.get("id")
            if seat_id is None:
                continue
            state = str(seat.get("state") or "")
            seat_type = str(seat.get("seat_type") or "")
            if state not in ("ready", "busy"):
                continue
            try:
                numeric_id = int(seat_id)
            except (TypeError, ValueError):
                continue
            if seat_type == "terminal":
                execs[numeric_id] = self._list_execs(client, numeric_id)
            elif seat_type == "desktop" and seat.get("vm_name"):
                shot = self._screenshot(client, numeric_id)
                if shot is not None:
                    screenshots[numeric_id] = shot
        if self._stop.is_set():
            return
        self.snapshotReady.emit(
            {"ok": True, "status": status, "execs": execs, "screenshots": screenshots}
        )

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

    def _screenshot(self, client: DaemonClient, seat_id: int) -> str | None:
        import base64

        try:
            data = client.screenshot(seat_id, max_width=self._screenshot_max_width)
        except DaemonClientError:
            return None
        if not data:
            return None
        return base64.b64encode(data).decode("ascii")


__all__ = ["ACTION_TIMEOUT_S", "POLL_REQUEST_TIMEOUT_S", "PollWorker", "WallBackend"]
