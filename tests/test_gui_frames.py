"""Frame-source + image-provider tests (package C), no VM.

A tiny in-process RFB server exercises the full ``VncFrameSource`` path
(connect -> handshake -> Raw frames -> QImage) so the unit suite stays
VM-free. ``StillsFrameSource`` and the provider are covered directly.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QBuffer, QSize  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402

from omavroom.gui.frames import (  # noqa: E402
    FrameImageProvider,
    StillsFrameSource,
    VncFrameSource,
)
from omavroom.gui.rfb import ENCODING_RAW, RFB_VERSION_3_8  # noqa: E402


def _png(width: int, height: int, color: int) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(color)
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def _server_init(width: int, height: int) -> bytes:
    pixel_format = struct.pack(
        ">BBBBHHHBBB3s", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0, b"\x00\x00\x00"
    )
    return struct.pack(">HH", width, height) + pixel_format + struct.pack(">I", 0)


def _read_exact(conn: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = conn.recv(count - len(buf))
        if not chunk:
            raise ConnectionError("client closed")
        buf += chunk
    return bytes(buf)


class _FakeVncServer(threading.Thread):
    """One-connection RFB server: handshake, then one raw update per request."""

    def __init__(self, width: int, height: int) -> None:
        super().__init__(daemon=True)
        self.width = width
        self.height = height
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.requests: list[tuple[int, int, int, int, int]] = []
        self._stop_event = threading.Event()

    def run(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2.0)
            try:
                self._serve(conn)
            except (ConnectionError, OSError, TimeoutError):
                pass

    def _serve(self, conn: socket.socket) -> None:
        conn.sendall(RFB_VERSION_3_8)
        _read_exact(conn, 12)  # client version
        conn.sendall(b"\x01\x01")  # one security type: None
        _read_exact(conn, 1)  # client selection
        conn.sendall(b"\x00\x00\x00\x00")  # security result: ok
        _read_exact(conn, 1)  # ClientInit (shared flag)
        conn.sendall(_server_init(self.width, self.height))
        counter = 0
        while not self._stop_event.is_set():
            message_type = _read_exact(conn, 1)[0]
            if message_type == 2:  # SetEncodings
                _read_exact(conn, 1)  # padding
                count = struct.unpack(">H", _read_exact(conn, 2))[0]
                _read_exact(conn, count * 4)
            elif message_type == 3:  # FramebufferUpdateRequest
                incremental, x, y, width, height = struct.unpack(">BHHHH", _read_exact(conn, 9))
                self.requests.append((incremental, x, y, width, height))
                counter += 1
                conn.sendall(self._raw_update(counter))

    def _raw_update(self, counter: int) -> bytes:
        # A flat rect whose red channel varies per frame, so tests can watch
        # the revision advance while pixels actually change.
        red = counter % 256
        pixel = bytes((0, 0, red, 0))  # little-endian B,G,R,pad
        data = pixel * (self.width * self.height)
        header = b"\x00\x00" + struct.pack(">H", 1)
        rect = struct.pack(">HHHHi", 0, 0, self.width, self.height, ENCODING_RAW)
        return header + rect + data

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._listener.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# StillsFrameSource
# --------------------------------------------------------------------------
class _FakeClient:
    def __init__(self, png: bytes) -> None:
        self.png = png
        self.calls: list[tuple[int, int]] = []

    def screenshot(self, seat_id: int, *, max_width: int | None = None) -> bytes:
        self.calls.append((int(seat_id), int(max_width or 0)))
        return self.png


def test_stills_source_lifecycle_and_revisions():
    source = StillsFrameSource(_FakeClient(_png(4, 2, 0xFF0000)))
    assert source.frame(1) is None and source.revision(1) == 0
    source.start(1, 320)
    image = source.frame(1)
    assert image is not None and (image.width(), image.height()) == (4, 2)
    assert source.revision(1) == 1
    source.frame(1)
    assert source.revision(1) == 2
    source.stop(1)
    assert source.frame(1) is None


def test_stills_source_records_fetch_errors():
    class _Boom:
        def screenshot(self, seat_id, *, max_width=None):
            raise RuntimeError("no daemon")

    source = StillsFrameSource(_Boom())
    source.start(1, 100)
    assert source.frame(1) is None
    assert source.error(1) == "no daemon"


# --------------------------------------------------------------------------
# VncFrameSource against the fake server
# --------------------------------------------------------------------------
def test_vnc_source_streams_raw_frames_and_revision_advances():
    server = _FakeVncServer(8, 4)
    server.start()
    source = VncFrameSource(connect_timeout=2.0, read_timeout=0.2)
    try:
        source.start(7, 1024, f"vnc://127.0.0.1:{server.port}")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and source.revision(7) < 2:
            time.sleep(0.02)
        image = source.frame(7)
        assert image is not None, f"no frame (error={source.error(7)})"
        assert (image.width(), image.height()) == (8, 4)
        assert source.revision(7) >= 2
        assert source.error(7) is None
        # The first (non-incremental) request must cover the whole framebuffer.
        assert server.requests[0] == (0, 0, 0, 8, 4)
        assert all(request[3:] == (8, 4) for request in server.requests)
    finally:
        source.stop(7)
        server.stop()


def test_vnc_source_reports_connection_failure():
    source = VncFrameSource(connect_timeout=0.5, read_timeout=0.2)
    # Nothing listens on port 1; the session records the failure.
    source.start(3, 800, "vnc://127.0.0.1:1")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and source.error(3) is None:
        time.sleep(0.02)
    assert source.error(3)
    source.stop(3)


# --------------------------------------------------------------------------
# FrameImageProvider
# --------------------------------------------------------------------------
def test_image_provider_returns_latest_frame_and_placeholder():
    server = _FakeVncServer(6, 3)
    server.start()
    source = VncFrameSource(connect_timeout=2.0, read_timeout=0.2)
    try:
        source.start(11, 512, f"vnc://127.0.0.1:{server.port}")
        provider = FrameImageProvider(source)
        deadline = time.monotonic() + 5.0
        size = QSize()
        image = None
        while time.monotonic() < deadline:
            image = provider.requestImage("11?v=1", size, QSize())
            if image.width() == 6:
                break
            time.sleep(0.02)
        assert (image.width(), image.height()) == (6, 3)
        assert (size.width(), size.height()) == (6, 3)
        # Unknown/missing seats get a 1x1 transparent placeholder, never None.
        placeholder = provider.requestImage("999", QSize(), QSize())
        assert (placeholder.width(), placeholder.height()) == (1, 1)
    finally:
        source.stop(11)
        server.stop()
