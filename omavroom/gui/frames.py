"""Frame sources and the QML image provider for live/downscaled monitors.

Package C introduces an **opt-in** real-time path (``gui.live_mode = "vnc"``)
for the *focused* desktop monitor. The scheduling seam is shared with the
existing stills path:

- :class:`FrameSource` is the protocol the wall uses: ``start``/``stop`` a
  seat, read its latest :class:`~PySide6.QtGui.QImage` with :meth:`frame`, and
  read a monotonic :meth:`revision` counter used to bust QML's image cache.
- :class:`StillsFrameSource` wraps ``DaemonClient.screenshot`` and is the
  default/fallback source; it keeps the existing stills semantics.
- :class:`VncFrameSource` connects to a seat's ``vnc://127.0.0.1:PORT``
  endpoint and streams updates on a background thread using the pure-Python
  :mod:`omavroom.gui.rfb` client, keeping the latest decoded frame per seat.

:class:`FrameImageProvider` serves ``image://omavroom/<seat_id>?v=<rev>`` from
whatever source the backend owns. The source thread writes an immutable
``QImage`` under a lock; the render thread reads a reference under the same
lock. No multi-megabyte base64 ever crosses the queued snapshot signal for the
live path.

The VNC thread is deliberately self-contained: it resolves its endpoint (via
an injected ``endpoint_for`` or a seat id handed in ``start``), reconnects
never, and records any failure on the session so the GUI can fall back to
stills with a clear notice instead of blocking or crashing.
"""

from __future__ import annotations

import socket
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from omavroom.gui.rfb import (
    ENCODING_DESKTOP_SIZE,
    ENCODING_RAW,
    Framebuffer,
    RfbClient,
    RfbError,
    RfbStopped,
    parse_endpoint,
)


class FrameSource(Protocol):
    """Latest-frame seam shared by the stills and VNC capture paths."""

    def start(self, seat_id: int, width: int, endpoint: str | None = None) -> None:
        """Begin (or restart) providing frames for ``seat_id`` at ``width``."""

    def stop(self, seat_id: int) -> None:
        """Stop providing frames for ``seat_id`` and release its resources."""

    def frame(self, seat_id: int) -> QImage | None:
        """The most recent frame for ``seat_id``, or ``None`` if unavailable."""

    def revision(self, seat_id: int) -> int:
        """Monotonic per-seat frame counter (0 when never provided)."""

    def error(self, seat_id: int) -> str | None:
        """A terminal error for ``seat_id`` (used to fall back to stills)."""


class StillsFrameSource:
    """A :class:`FrameSource` backed by ``DaemonClient.screenshot``.

    The production stills path is unchanged (the poll worker still ships
    base64 frames through the snapshot signal); this adapter exists so the
    provider/revision seam and the live/fallback wiring are testable and so a
    seat can degrade to stills through the same interface.
    """

    def __init__(self, client) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._widths: dict[int, int] = {}
        self._frames: dict[int, QImage] = {}
        self._revisions: dict[int, int] = {}
        self._errors: dict[int, str] = {}

    def start(self, seat_id: int, width: int, endpoint: str | None = None) -> None:
        seat = int(seat_id)
        with self._lock:
            self._widths[seat] = max(1, int(width))
            self._revisions.setdefault(seat, 0)
            self._errors.pop(seat, None)

    def stop(self, seat_id: int) -> None:
        with self._lock:
            self._widths.pop(int(seat_id), None)

    def frame(self, seat_id: int) -> QImage | None:
        seat = int(seat_id)
        with self._lock:
            width = self._widths.get(seat)
        if width is None:
            return None
        try:
            data = self._client.screenshot(seat, max_width=width)
        except Exception as exc:  # noqa: BLE001 - a frame fetch must never raise into Qt
            with self._lock:
                self._errors[seat] = str(exc)
            return self._cached(seat)
        image = QImage.fromData(bytes(data), "PNG") if data else QImage()
        if image.isNull():
            return self._cached(seat)
        with self._lock:
            self._frames[seat] = image
            self._revisions[seat] = self._revisions.get(seat, 0) + 1
        return image

    def revision(self, seat_id: int) -> int:
        with self._lock:
            return self._revisions.get(int(seat_id), 0)

    def error(self, seat_id: int) -> str | None:
        with self._lock:
            return self._errors.get(int(seat_id))

    def _cached(self, seat_id: int) -> QImage | None:
        with self._lock:
            return self._frames.get(seat_id)


class _VncSession(threading.Thread):
    """One RFB stream: connect, handshake, then decode frames until stopped."""

    def __init__(self, host: str, port: int, connect_timeout: float, read_timeout: float) -> None:
        super().__init__(daemon=True, name=f"omavroom-vnc-{host}:{port}")
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._frame: QImage | None = None
        self._revision = 0
        self._error: str | None = None

    def run(self) -> None:
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((self._host, self._port), timeout=self._connect_timeout)
            self._sock = sock
            sock.settimeout(self._read_timeout)
            client = RfbClient(sock)
            init = client.handshake()
            client.set_encodings([ENCODING_RAW, ENCODING_DESKTOP_SIZE])
            framebuffer = Framebuffer(init.width, init.height, init.pixel_format)
            client.request_update(incremental=False, width=init.width, height=init.height)
            while not self._stop_event.is_set():
                update = client.read_message(self._stop_event)
                if update is None:
                    continue
                if framebuffer.apply(update):
                    rgba = framebuffer.to_rgba()
                    image = QImage(
                        rgba,
                        framebuffer.width,
                        framebuffer.height,
                        framebuffer.width * 4,
                        QImage.Format.Format_RGBA8888,
                    ).copy()
                    with self._lock:
                        self._frame = image
                        self._revision += 1
                # Always ask for the next increment: an empty/no-op update must
                # not stall the stream waiting for a frame that never comes.
                client.request_update(
                    incremental=True,
                    width=framebuffer.width,
                    height=framebuffer.height,
                )
        except RfbStopped:
            pass
        except Exception as exc:  # noqa: BLE001 - publish, never propagate off-thread
            if not self._stop_event.is_set():
                with self._lock:
                    self._error = str(exc)
        finally:
            self._sock = None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop_event.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def frame(self) -> QImage | None:
        with self._lock:
            return self._frame

    def revision(self) -> int:
        with self._lock:
            return self._revision

    def error(self) -> str | None:
        with self._lock:
            return self._error


class VncFrameSource:
    """Streams the focused seat's framebuffer over VNC (Raw encoding).

    Endpoints may be passed to :meth:`start` (the normal path: the worker
    resolves ``DaemonClient.peek_endpoint``) or resolved lazily through an
    injected ``endpoint_for`` callable. Each seat gets one session thread.
    """

    def __init__(
        self,
        *,
        endpoint_for: Callable[[int], str] | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 0.5,
        stop_join_timeout: float = 2.0,
    ) -> None:
        self._endpoint_for = endpoint_for
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._stop_join_timeout = stop_join_timeout
        self._lock = threading.Lock()
        self._sessions: dict[int, _VncSession] = {}
        #: Last good frame per seat, kept after ``stop`` so the image provider
        #: still has something to show on fallback instead of a blank tile.
        self._last_frames: dict[int, QImage] = {}

    def start(self, seat_id: int, width: int, endpoint: str | None = None) -> None:
        seat = int(seat_id)
        with self._lock:
            existing = self._sessions.get(seat)
        if existing is not None and existing.is_alive():
            return
        if endpoint is None:
            if self._endpoint_for is None:
                raise RfbError("no VNC endpoint supplied and no resolver configured")
            endpoint = self._endpoint_for(seat)
        host, port = parse_endpoint(endpoint)
        session = _VncSession(host, port, self._connect_timeout, self._read_timeout)
        with self._lock:
            self._sessions[seat] = session
        session.start()

    def stop(self, seat_id: int) -> None:
        seat = int(seat_id)
        with self._lock:
            session = self._sessions.pop(seat, None)
        if session is not None:
            session.stop()
            session.join(timeout=self._stop_join_timeout)
            # Retain the last decoded frame: stopping must not blank the tile.
            frame = session.frame()
            if frame is not None and not frame.isNull():
                with self._lock:
                    self._last_frames[seat] = frame

    def stop_all(self) -> None:
        with self._lock:
            seats = list(self._sessions)
        for seat in seats:
            self.stop(seat)

    def frame(self, seat_id: int) -> QImage | None:
        seat = int(seat_id)
        with self._lock:
            session = self._sessions.get(seat)
            last = self._last_frames.get(seat)
        if session is not None:
            frame = session.frame()
            if frame is not None:
                return frame
        return last

    def revision(self, seat_id: int) -> int:
        with self._lock:
            session = self._sessions.get(int(seat_id))
        return session.revision() if session is not None else 0

    def error(self, seat_id: int) -> str | None:
        with self._lock:
            session = self._sessions.get(int(seat_id))
        return session.error() if session is not None else None


def _placeholder_image() -> QImage:
    """A 1x1 transparent image so an unresolved tile never renders stale art."""
    image = QImage(1, 1, QImage.Format.Format_ARGB32)
    image.fill(0)
    return image


def _seat_id_from_image_id(image_id: str) -> int | None:
    """Extract the seat id from ``"<seat_id>?v=<rev>"`` (query optional)."""
    text = str(image_id or "").split("?", 1)[0].strip()
    try:
        return int(text)
    except ValueError:
        return None


#: How many per-seat frames the provider remembers once a source goes away.
#: Small: only the focused seat (and a little history) is ever requested.
_MAX_CACHED_FRAMES = 8


class FrameImageProvider(QQuickImageProvider):
    """Serves the live frame for ``image://omavroom/<seat_id>?v=<rev>``.

    Called on the render thread while the source's own thread publishes new
    frames, so every access goes through the source's lock (``frame`` /
    ``revision`` are safe to call concurrently).

    The last frame successfully served per seat is cached here as well: if the
    source forgets it (a stream was stopped/replaced), the provider still
    returns the most recent frame instead of a blank placeholder. Only a seat
    that has *never* produced a frame gets the 1x1 transparent placeholder.
    """

    def __init__(self, source: FrameSource) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._source = source
        self._lock = threading.Lock()
        self._last: OrderedDict[int, QImage] = OrderedDict()

    def requestImage(self, image_id: str, size: QSize, requested_size: QSize) -> QImage:
        """Qt virtual: return the latest frame for the requested seat id."""
        seat_id = _seat_id_from_image_id(image_id)
        image = self._source.frame(seat_id) if seat_id is not None else None
        if image is not None and not image.isNull() and seat_id is not None:
            with self._lock:
                self._last[seat_id] = image
                self._last.move_to_end(seat_id)
                while len(self._last) > _MAX_CACHED_FRAMES:
                    self._last.popitem(last=False)
        elif seat_id is not None:
            with self._lock:
                image = self._last.get(seat_id)
        if image is None or image.isNull():
            image = _placeholder_image()
        size.setWidth(image.width())
        size.setHeight(image.height())
        return image


__all__ = [
    "FrameImageProvider",
    "FrameSource",
    "StillsFrameSource",
    "VncFrameSource",
]
