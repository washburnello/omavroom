"""Pure-Python RFB client tests (package C): parsing and Raw decoding.

No VM, no Qt: synthetic bytes drive the handshake, ServerInit, framebuffer
updates and pixel-format conversion through an injected fake socket.
"""

from __future__ import annotations

import struct
import threading

import pytest

from omavroom.gui.rfb import (
    ENCODING_DESKTOP_SIZE,
    ENCODING_RAW,
    RFB_VERSION_3_3,
    RFB_VERSION_3_7,
    RFB_VERSION_3_8,
    Framebuffer,
    FramebufferUpdate,
    PixelFormat,
    Rect,
    RfbClient,
    RfbError,
    RfbStopped,
    parse_endpoint,
    parse_pixel_format,
    parse_server_init,
    parse_version,
)


def _pf32(
    *,
    big_endian: bool = False,
    red_shift: int = 16,
    green_shift: int = 8,
    blue_shift: int = 0,
) -> PixelFormat:
    return PixelFormat(
        bpp=32,
        depth=24,
        big_endian=big_endian,
        true_colour=True,
        red_max=255,
        green_max=255,
        blue_max=255,
        red_shift=red_shift,
        green_shift=green_shift,
        blue_shift=blue_shift,
    )


def _pf32_bytes(**kwargs) -> bytes:
    pf = _pf32(**kwargs)
    return struct.pack(
        ">BBBBHHHBBB3s",
        pf.bpp,
        pf.depth,
        int(pf.big_endian),
        int(pf.true_colour),
        pf.red_max,
        pf.green_max,
        pf.blue_max,
        pf.red_shift,
        pf.green_shift,
        pf.blue_shift,
        b"\x00\x00\x00",
    )


def _server_init(
    width: int, height: int, name: str = "seat", pf_bytes: bytes | None = None
) -> bytes:
    pf_bytes = pf_bytes if pf_bytes is not None else _pf32_bytes()
    encoded = name.encode()
    return struct.pack(">HH", width, height) + pf_bytes + struct.pack(">I", len(encoded)) + encoded


def _pixel_le(r: int, g: int, b: int) -> bytes:
    # 32bpp little-endian with shifts R16/G8/B0 -> memory order B,G,R,pad.
    return bytes((b, g, r, 0))


class FakeSocket:
    """A scripted socket: yields queued bytes, records everything sent."""

    def __init__(self, incoming: bytes = b"") -> None:
        self._incoming = bytearray(incoming)
        self.sent = bytearray()

    def recv(self, bufsize: int) -> bytes:
        if not self._incoming:
            return b""
        chunk = bytes(self._incoming[:bufsize])
        del self._incoming[:bufsize]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent += data


# --------------------------------------------------------------------------
# pure parsers
# --------------------------------------------------------------------------
def test_parse_version_accepts_known_banners():
    assert parse_version(RFB_VERSION_3_8) == (3, 8)
    assert parse_version(RFB_VERSION_3_7) == (3, 7)
    assert parse_version(RFB_VERSION_3_3) == (3, 3)


def test_parse_version_rejects_garbage():
    with pytest.raises(RfbError):
        parse_version(b"nope")


def test_parse_pixel_format_round_trips():
    pf = parse_pixel_format(_pf32_bytes(big_endian=True, red_shift=0, blue_shift=16))
    assert pf.bpp == 32 and pf.depth == 24
    assert pf.big_endian is True and pf.true_colour is True
    assert (pf.red_shift, pf.green_shift, pf.blue_shift) == (0, 8, 16)
    assert pf.bytes_per_pixel == 4 and pf.is_fast_8bit()


def test_parse_pixel_format_requires_16_bytes():
    with pytest.raises(RfbError):
        parse_pixel_format(b"\x00" * 15)


def test_parse_server_init_reads_size_and_name():
    pf = _pf32()
    init = parse_server_init(_server_init(1280, 800, "seat", _pf32_bytes()), pf, "seat")
    assert (init.width, init.height) == (1280, 800)
    assert init.name == "seat"
    assert init.pixel_format is pf


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("vnc://127.0.0.1:5900", ("127.0.0.1", 5900)),
        ("127.0.0.1:5901", ("127.0.0.1", 5901)),
        ("vnc://host", ("host", 5900)),
        ("[::1]:5902", ("::1", 5902)),
    ],
)
def test_parse_endpoint(endpoint, expected):
    assert parse_endpoint(endpoint) == expected


def test_parse_endpoint_rejects_empty_and_bad_port():
    with pytest.raises(RfbError):
        parse_endpoint("")
    with pytest.raises(RfbError):
        parse_endpoint("host:not-a-port")


# --------------------------------------------------------------------------
# handshake
# --------------------------------------------------------------------------
def _handshake_bytes(version: bytes, security: bytes, server_init: bytes) -> bytes:
    return version + security + server_init


def test_handshake_rfb38_none_security():
    sock = FakeSocket(
        _handshake_bytes(
            RFB_VERSION_3_8, b"\x01\x01" + b"\x00\x00\x00\x00", _server_init(1280, 800)
        )
    )
    client = RfbClient(sock)
    init = client.handshake()
    assert (init.width, init.height) == (1280, 800)
    assert init.pixel_format.is_fast_8bit()
    # Reply version + security type None + ClientInit shared flag.
    assert bytes(sock.sent) == RFB_VERSION_3_8 + b"\x01" + b"\x01"
    assert client.pixel_format is init.pixel_format


def test_handshake_rfb37_none_security_has_no_result_word():
    sock = FakeSocket(_handshake_bytes(RFB_VERSION_3_7, b"\x01\x01", _server_init(640, 480)))
    init = RfbClient(sock).handshake()
    assert (init.width, init.height) == (640, 480)


def test_handshake_rfb33_none_security():
    sock = FakeSocket(
        _handshake_bytes(RFB_VERSION_3_3, struct.pack(">I", 1), _server_init(800, 600))
    )
    init = RfbClient(sock).handshake()
    assert (init.width, init.height) == (800, 600)


def test_handshake_rejects_authentication_requirement():
    sock = FakeSocket(_handshake_bytes(RFB_VERSION_3_8, b"\x01\x02", b""))
    with pytest.raises(RfbError, match="authentication"):
        RfbClient(sock).handshake()


def test_handshake_rejects_unsupported_version():
    sock = FakeSocket(b"RFB 003.889\n")
    with pytest.raises(RfbError, match="unsupported RFB version"):
        RfbClient(sock).handshake()


def test_handshake_reports_server_refusal():
    reason = b"too many clients"
    payload = RFB_VERSION_3_8 + b"\x00" + struct.pack(">I", len(reason)) + reason
    with pytest.raises(RfbError, match="refused"):
        RfbClient(FakeSocket(payload)).handshake()


def test_set_encodings_and_request_update_wire_format():
    sock = FakeSocket()
    client = RfbClient(sock)
    client.set_encodings([ENCODING_RAW, ENCODING_DESKTOP_SIZE])
    assert bytes(sock.sent) == b"\x02\x00" + struct.pack(">Hii", 2, 0, -223)
    sock.sent.clear()
    client.request_update(incremental=True, width=1280, height=800)
    assert bytes(sock.sent) == struct.pack(">BBHHHH", 3, 1, 0, 0, 1280, 800)


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------
def _raw_update(rects: list[tuple[int, int, int, int, bytes]]) -> bytes:
    payload = bytearray(b"\x00\x00" + struct.pack(">H", len(rects)))
    for x, y, w, h, data in rects:
        payload += struct.pack(">HHHHi", x, y, w, h, ENCODING_RAW)
        payload += data
    return bytes(payload)


def _client_with_format(pf: PixelFormat | None = None) -> tuple[RfbClient, FakeSocket]:
    sock = FakeSocket()
    client = RfbClient(sock)
    client.pixel_format = pf or _pf32()
    return client, sock


def test_read_raw_framebuffer_update():
    data = _pixel_le(255, 0, 0) + _pixel_le(0, 255, 0) + _pixel_le(0, 0, 255) + _pixel_le(1, 2, 3)
    client, _ = _client_with_format()
    client._sock = FakeSocket(_raw_update([(0, 0, 2, 2, data)]))
    update = client.read_message()
    assert update is not None and len(update.rects) == 1
    rect = update.rects[0]
    assert (rect.x, rect.y, rect.width, rect.height, rect.encoding) == (0, 0, 2, 2, ENCODING_RAW)
    assert rect.data == data


def test_read_message_skips_bell_colour_map_and_cut_text():
    bell = b"\x02"
    colour_map = b"\x01\x00" + struct.pack(">HH", 1, 1) + struct.pack(">HHH", 1, 2, 3)
    cut_text = b"\x03\x00\x00\x00" + struct.pack(">I", 3) + b"hey"
    client, _ = _client_with_format()
    client._sock = FakeSocket(bell + colour_map + cut_text)
    assert client.read_message() is None
    assert client.read_message() is None
    assert client.read_message() is None


def test_read_message_rejects_unrequested_encoding():
    header = b"\x00\x00" + struct.pack(">H", 1)
    rect = struct.pack(">HHHHi", 0, 0, 1, 1, 5)  # 5 = Hextile, never requested
    client, _ = _client_with_format()
    client._sock = FakeSocket(header + rect)
    with pytest.raises(RfbError, match="unrequested encoding"):
        client.read_message()


def test_read_message_stop_event_raises_before_blocking():
    client, _ = _client_with_format()
    stop = threading.Event()
    stop.set()
    with pytest.raises(RfbStopped):
        client.read_message(stop)


# --------------------------------------------------------------------------
# framebuffer compositing + conversion
# --------------------------------------------------------------------------
def test_framebuffer_fast_path_decodes_rgba():
    fb = Framebuffer(2, 1, _pf32())
    fb.apply(
        FramebufferUpdate(
            (Rect(0, 0, 2, 1, ENCODING_RAW, _pixel_le(10, 20, 30) + _pixel_le(1, 2, 3)),)
        )
    )
    assert fb.to_rgba() == bytes((10, 20, 30, 255, 1, 2, 3, 255))


def test_framebuffer_incremental_blit_and_resize():
    fb = Framebuffer(3, 3, _pf32())
    fb.apply(FramebufferUpdate((Rect(1, 1, 2, 2, ENCODING_RAW, _pixel_le(255, 0, 0) * 4),)))
    rgba = fb.to_rgba()
    center = rgba[(1 * 3 + 1) * 4 : (1 * 3 + 1) * 4 + 4]
    assert center == bytes((255, 0, 0, 255))
    # A DesktopSize pseudo-rect resizes the buffer without pixel data.
    fb.apply(FramebufferUpdate((Rect(0, 0, 5, 4, ENCODING_DESKTOP_SIZE),)))
    assert (fb.width, fb.height) == (5, 4)
    assert len(fb.to_rgba()) == 5 * 4 * 4


def test_framebuffer_big_endian_channel_order():
    # Big-endian 32bpp: byte 0 is the MSB, so R16/G8/B0 lands at bytes 1/2/3.
    fb = Framebuffer(1, 1, _pf32(big_endian=True))
    fb.apply(FramebufferUpdate((Rect(0, 0, 1, 1, ENCODING_RAW, bytes((0, 30, 20, 10))),)))
    assert fb.to_rgba() == bytes((30, 20, 10, 255))


def test_framebuffer_general_16bpp_565_fallback():
    pf = PixelFormat(
        bpp=16,
        depth=16,
        big_endian=False,
        true_colour=True,
        red_max=31,
        green_max=63,
        blue_max=31,
        red_shift=11,
        green_shift=5,
        blue_shift=0,
    )
    fb = Framebuffer(1, 1, pf)
    fb.apply(FramebufferUpdate((Rect(0, 0, 1, 1, ENCODING_RAW, struct.pack("<H", 0xF800)),)))
    assert fb.to_rgba() == bytes((255, 0, 0, 255))


def test_framebuffer_rejects_colour_map_formats():
    pf = PixelFormat(8, 8, False, False, 0, 0, 0, 0, 0, 0)
    fb = Framebuffer(1, 1, pf)
    with pytest.raises(RfbError, match="colour-map"):
        fb.to_rgba()


def test_framebuffer_rejects_out_of_bounds_rect():
    fb = Framebuffer(2, 2, _pf32())
    with pytest.raises(RfbError, match="out of bounds"):
        fb.apply(FramebufferUpdate((Rect(1, 0, 2, 1, ENCODING_RAW, _pixel_le(0, 0, 0) * 2),)))
