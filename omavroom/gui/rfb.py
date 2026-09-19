"""Minimal, dependency-free RFB (VNC) client for live seat frames (package C).

Desktop seats expose a passwordless VNC server bound to ``127.0.0.1``
(``virsh domdisplay`` / ``peek_endpoint`` -> ``vnc://127.0.0.1:PORT``). This
module speaks just enough RFB to stream the framebuffer:

- handshake across RFB 3.3 / 3.7 / 3.8 with security type **None**;
- :class:`ServerInit` (size + server pixel format) and ``SetEncodings``;
- **Raw** (0) and **DesktopSize** (-223) encodings only;
- incremental ``FramebufferUpdateRequest``s;
- server messages ``FramebufferUpdate``/``SetColourMapEntries``/``Bell``/
  ``ServerCutText`` (unknown/other encodings raise, they are never requested).

Decoding is split from I/O so it is unit-testable with synthetic bytes:

- :func:`parse_version`, :func:`parse_pixel_format`,
  :func:`parse_server_init` are pure parsers;
- :class:`Framebuffer` composites raw rects and converts to RGBA bytes;
- :class:`RfbClient` drives handshake/messages over any socket-like object
  (``recv``/``sendall``), so tests inject a fake socket.

Limits (reported, not silently wrong): only true-colour pixel formats are
decoded. The 32bpp little/big-endian 8-bit-channel layout (the common QEMU
case) takes a fast slice path; other true-colour formats (16bpp 5:6:5, etc.)
use a correct but slower per-pixel path. Colour-map formats raise
:class:`RfbError`. No authentication, no other encodings, no zlib/hextile.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Protocol

#: RFB protocol versions this client understands and its reply (always 3.8).
RFB_VERSION_3_3 = b"RFB 003.003\n"
RFB_VERSION_3_7 = b"RFB 003.007\n"
RFB_VERSION_3_8 = b"RFB 003.008\n"
_KNOWN_VERSIONS = (RFB_VERSION_3_3, RFB_VERSION_3_7, RFB_VERSION_3_8)

#: Security type ``None`` (no authentication).
SECURITY_NONE = 1

#: Encodings requested from the server.
ENCODING_RAW = 0
#: Pseudo-encoding: the server reports a framebuffer resize.
ENCODING_DESKTOP_SIZE = -223

#: Server-to-client message types.
MSG_FRAMEBUFFER_UPDATE = 0
MSG_SET_COLOUR_MAP_ENTRIES = 1
MSG_BELL = 2
MSG_SERVER_CUT_TEXT = 3


class RfbError(Exception):
    """Any RFB protocol or transport failure."""


class RfbStopped(RfbError):
    """Raised internally when a cooperative stop interrupts a blocking read."""


@dataclass(frozen=True)
class PixelFormat:
    """The server's 16-byte pixel-format descriptor (RFB 7.5)."""

    bpp: int
    depth: int
    big_endian: bool
    true_colour: bool
    red_max: int
    green_max: int
    blue_max: int
    red_shift: int
    green_shift: int
    blue_shift: int

    @property
    def bytes_per_pixel(self) -> int:
        """Bytes per framebuffer pixel (``bpp`` is always a byte multiple)."""
        return max(1, self.bpp // 8)

    def is_fast_8bit(self) -> bool:
        """True for the common 32bpp, 8-bit-per-channel layout.

        QEMU's default: 32bpp, depth 24, 8-bit channels. The conversion then
        reduces to a byte-channel gather + shuffle, which is fast in C-level
        slice assignments.
        """
        return (
            self.bpp == 32
            and self.true_colour
            and self.red_max == 255
            and self.green_max == 255
            and self.blue_max == 255
        )


@dataclass(frozen=True)
class ServerInit:
    """The server's opening description: framebuffer size and pixel format."""

    width: int
    height: int
    pixel_format: PixelFormat
    name: str


@dataclass(frozen=True)
class Rect:
    """One update rectangle; ``data`` is the raw payload for Raw encoding."""

    x: int
    y: int
    width: int
    height: int
    encoding: int
    data: bytes = b""


@dataclass(frozen=True)
class FramebufferUpdate:
    """A decoded ``FramebufferUpdate`` (possibly only a resize/reset marker)."""

    rects: tuple[Rect, ...]


class SocketLike(Protocol):
    """The slice of ``socket.socket`` this client uses."""

    def recv(self, bufsize: int) -> bytes: ...

    def sendall(self, data: bytes) -> None: ...


def parse_version(data: bytes) -> tuple[int, int]:
    """Parse a 12-byte ``RFB 003.008\\n`` banner into ``(major, minor)``."""
    if len(data) != 12 or not data.startswith(b"RFB "):
        raise RfbError(f"not an RFB version banner: {data!r}")
    try:
        parts = data[4:].strip().split(b".")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError) as exc:
        raise RfbError(f"malformed RFB version banner: {data!r}") from exc


def parse_pixel_format(data: bytes) -> PixelFormat:
    """Parse the 16-byte pixel-format block (bpp/depth/flags/maxes/shifts)."""
    if len(data) != 16:
        raise RfbError(f"pixel format must be 16 bytes, got {len(data)}")
    bpp, depth, big_endian, true_colour = data[0], data[1], data[2], data[3]
    red_max, green_max, blue_max = struct.unpack(">HHH", data[4:10])
    red_shift, green_shift, blue_shift = data[10], data[11], data[12]
    return PixelFormat(
        bpp=bpp,
        depth=depth,
        big_endian=bool(big_endian),
        true_colour=bool(true_colour),
        red_max=red_max,
        green_max=green_max,
        blue_max=blue_max,
        red_shift=red_shift,
        green_shift=green_shift,
        blue_shift=blue_shift,
    )


def parse_server_init(data: bytes, pixel_format: PixelFormat, name: str = "") -> ServerInit:
    """Parse the 24-byte ServerInit header given its already-parsed format."""
    if len(data) < 24:
        raise RfbError(f"ServerInit must be >= 24 bytes, got {len(data)}")
    width, height = struct.unpack(">HH", data[0:4])
    return ServerInit(width=width, height=height, pixel_format=pixel_format, name=name)


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    """Split a ``vnc://127.0.0.1:5900`` (or ``host:port``) endpoint.

    Accepts an optional ``vnc://``/``tcp://`` scheme, a bracketed IPv6 host,
    and a bare ``host`` (port defaults to 5900). Raises :class:`RfbError`
    for an empty host or non-numeric port.
    """
    text = (endpoint or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.strip().strip("/")
    if not text:
        raise RfbError(f"empty VNC endpoint: {endpoint!r}")
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") and rest[1:] else 5900
        return host, port
    host, sep, port_text = text.rpartition(":")
    if not sep or not host:
        return text, 5900
    try:
        return host, int(port_text)
    except ValueError as exc:
        raise RfbError(f"invalid VNC endpoint port: {endpoint!r}") from exc


class Framebuffer:
    """A server-format framebuffer that composites Raw rects and exports RGBA.

    The native buffer is kept in the server's own pixel layout; conversion to
    8-bit RGBA happens once per published frame (not per rect), so an
    incremental update only pays for the pixels the server actually sent plus
    the export.
    """

    def __init__(self, width: int, height: int, pixel_format: PixelFormat) -> None:
        if width <= 0 or height <= 0:
            raise RfbError(f"invalid framebuffer size {width}x{height}")
        self.pixel_format = pixel_format
        self.width = width
        self.height = height
        self._bpp = pixel_format.bytes_per_pixel
        self._buf = bytearray(width * height * self._bpp)

    def apply(self, update: FramebufferUpdate) -> bool:
        """Composite an update; return whether any pixels changed.

        A DesktopSize rect resizes the buffer (copying the overlapping
        region), others are blitted. Rectangles outside the buffer are
        clamped/ignored rather than allowed to corrupt memory.
        """
        changed = False
        for rect in update.rects:
            if rect.encoding == ENCODING_DESKTOP_SIZE:
                if rect.width > 0 and rect.height > 0:
                    self._resize(rect.width, rect.height)
                    changed = True
                continue
            if rect.encoding != ENCODING_RAW or rect.width <= 0 or rect.height <= 0:
                continue
            self._blit(rect)
            changed = True
        return changed

    def _resize(self, width: int, height: int) -> None:
        new = bytearray(width * height * self._bpp)
        copy_w = min(width, self.width)
        copy_h = min(height, self.height)
        row_bytes = copy_w * self._bpp
        for row in range(copy_h):
            src = row * self.width * self._bpp
            dst = row * width * self._bpp
            new[dst : dst + row_bytes] = self._buf[src : src + row_bytes]
        self._buf = new
        self.width = width
        self.height = height

    def _blit(self, rect: Rect) -> None:
        row_bytes = rect.width * self._bpp
        if rect.x < 0 or rect.y < 0 or rect.x + rect.width > self.width:
            raise RfbError(f"raw rect {rect.x},{rect.y} {rect.width}x{rect.height} out of bounds")
        expected = row_bytes * rect.height
        if len(rect.data) < expected:
            raise RfbError(f"raw rect payload too short: {len(rect.data)} < {expected}")
        src = rect.data
        for row in range(rect.height):
            y = rect.y + row
            if y >= self.height:
                break
            dst = (y * self.width + rect.x) * self._bpp
            start = row * row_bytes
            self._buf[dst : dst + row_bytes] = src[start : start + row_bytes]

    def to_rgba(self) -> bytes:
        """Export the framebuffer as packed ``R,G,B,A`` bytes (row-major)."""
        pf = self.pixel_format
        if not pf.true_colour:
            raise RfbError("colour-map (non true-colour) pixel formats are unsupported")
        count = self.width * self.height
        out = bytearray(count * 4)
        if pf.is_fast_8bit():
            bpp = pf.bytes_per_pixel
            red = self._channel_byte(pf.red_shift, pf.big_endian, bpp)
            green = self._channel_byte(pf.green_shift, pf.big_endian, bpp)
            blue = self._channel_byte(pf.blue_shift, pf.big_endian, bpp)
            src = memoryview(self._buf)
            out[0::4] = src[red::bpp]
            out[1::4] = src[green::bpp]
            out[2::4] = src[blue::bpp]
        else:
            self._decode_general(out)
        out[3::4] = b"\xff" * count
        return bytes(out)

    @staticmethod
    def _channel_byte(shift: int, big_endian: bool, bpp: int) -> int:
        """Byte index holding a shift's channel, honouring endianness."""
        index = shift // 8
        return bpp - 1 - index if big_endian else index

    def _decode_general(self, out: bytearray) -> None:
        pf = self.pixel_format
        bpp = pf.bytes_per_pixel
        order = "big" if pf.big_endian else "little"
        rlut = [(v * 255) // pf.red_max if pf.red_max else 0 for v in range(pf.red_max + 1)]
        glut = [(v * 255) // pf.green_max if pf.green_max else 0 for v in range(pf.green_max + 1)]
        blut = [(v * 255) // pf.blue_max if pf.blue_max else 0 for v in range(pf.blue_max + 1)]
        buf = self._buf
        for i in range(self.width * self.height):
            off = i * bpp
            raw = int.from_bytes(buf[off : off + bpp], order)
            out[i * 4] = rlut[(raw >> pf.red_shift) & pf.red_max]
            out[i * 4 + 1] = glut[(raw >> pf.green_shift) & pf.green_max]
            out[i * 4 + 2] = blut[(raw >> pf.blue_shift) & pf.blue_max]


class RfbClient:
    """Drives one RFB connection over an injected socket-like object.

    ``read_message`` is blocking; when a ``stop`` event is supplied, a
    socket timeout loop lets it observe the flag without corrupting a
    partially-read message (it raises :class:`RfbStopped` at a message
    boundary).
    """

    def __init__(self, sock: SocketLike) -> None:
        self._sock = sock
        self.pixel_format: PixelFormat | None = None

    def _recv(self, count: int, stop=None) -> bytes:
        buf = bytearray()
        while len(buf) < count:
            if stop is not None and stop.is_set():
                raise RfbStopped("stop requested")
            try:
                chunk = self._sock.recv(count - len(buf))
            except TimeoutError:
                if stop is None:
                    raise RfbError("timed out waiting for VNC data") from None
                continue
            except OSError as exc:
                if stop is not None and stop.is_set():
                    raise RfbStopped("stop requested") from exc
                raise RfbError(f"VNC socket error: {exc}") from exc
            if not chunk:
                raise RfbError("VNC server closed the connection")
            buf += chunk
        return bytes(buf)

    def handshake(self) -> ServerInit:
        """Complete the version/security/ServerInit handshake (no auth)."""
        banner = self._recv(12)
        if banner not in _KNOWN_VERSIONS:
            parse_version(banner)  # raises with a clear message
            raise RfbError(f"unsupported RFB version {banner!r}")
        minor = parse_version(banner)[1]
        # Reply with 3.8; the server downgrades to the negotiated version.
        self._sock.sendall(RFB_VERSION_3_8)
        if minor >= 7:
            count = self._recv(1)[0]
            if count == 0:
                length = struct.unpack(">I", self._recv(4))[0]
                reason = self._recv(length).decode("utf-8", "replace")
                raise RfbError(f"VNC server refused the connection: {reason}")
            offered = self._recv(count)
            if SECURITY_NONE not in offered:
                raise RfbError(f"VNC server requires authentication (offered {list(offered)})")
            self._sock.sendall(bytes([SECURITY_NONE]))
            if minor >= 8:
                result = self._recv(4)
                if result != b"\x00\x00\x00\x00":
                    raise RfbError("VNC security handshake failed")
        else:
            security = struct.unpack(">I", self._recv(4))[0]
            if security != SECURITY_NONE:
                raise RfbError(f"VNC server requires authentication (type {security})")
        # ClientInit: shared-flag 1 (allow other viewers to stay connected).
        self._sock.sendall(b"\x01")
        header = self._recv(24)
        pixel_format = parse_pixel_format(header[4:20])
        name_length = struct.unpack(">I", header[20:24])[0]
        name = self._recv(name_length).decode("utf-8", "replace") if name_length else ""
        self.pixel_format = pixel_format
        return parse_server_init(header, pixel_format, name)

    def set_encodings(self, encodings: list[int] | tuple[int, ...]) -> None:
        """Advertise the encodings we accept (Raw and DesktopSize only).

        Wire format: type(1) + padding(1) + count(2) + count * encoding(4).
        The padding byte is mandatory; omitting it makes the server read the
        count from the wrong offset and stall waiting for phantom encodings.
        """
        payload = bytearray(struct.pack(">H", len(encodings)))
        for encoding in encodings:
            payload += struct.pack(">i", int(encoding))
        self._sock.sendall(b"\x02\x00" + bytes(payload))

    def request_update(
        self, *, incremental: bool, x: int = 0, y: int = 0, width: int = 0, height: int = 0
    ) -> None:
        """Send a FramebufferUpdateRequest for the given region."""
        self._sock.sendall(
            struct.pack(
                ">BBHHHH", 3, 1 if incremental else 0, int(x), int(y), int(width), int(height)
            )
        )

    def read_message(self, stop=None) -> FramebufferUpdate | None:
        """Read one server message; returns ``None`` for ignorable ones."""
        message_type = self._recv(1, stop)[0]
        if message_type == MSG_FRAMEBUFFER_UPDATE:
            return self._read_framebuffer_update(stop)
        if message_type == MSG_SET_COLOUR_MAP_ENTRIES:
            header = self._recv(5, stop)
            count = struct.unpack(">H", header[3:5])[0]
            self._recv(count * 6, stop)
            return None
        if message_type == MSG_BELL:
            return None
        if message_type == MSG_SERVER_CUT_TEXT:
            header = self._recv(7, stop)
            length = struct.unpack(">I", header[3:7])[0]
            self._recv(length, stop)
            return None
        raise RfbError(f"unknown server message type {message_type}")

    def _read_framebuffer_update(self, stop) -> FramebufferUpdate:
        if self.pixel_format is None:
            raise RfbError("FramebufferUpdate before ServerInit")
        header = self._recv(3, stop)
        count = struct.unpack(">H", header[1:3])[0]
        rects: list[Rect] = []
        for _ in range(count):
            raw_header = self._recv(12, stop)
            x, y, width, height, encoding = struct.unpack(">HHHHi", raw_header)
            data = b""
            if encoding == ENCODING_RAW:
                size = width * height * self.pixel_format.bytes_per_pixel
                data = self._recv(size, stop)
            elif encoding != ENCODING_DESKTOP_SIZE:
                raise RfbError(f"server sent unrequested encoding {encoding}")
            rects.append(Rect(x, y, width, height, encoding, data))
        return FramebufferUpdate(tuple(rects))


__all__ = [
    "ENCODING_DESKTOP_SIZE",
    "ENCODING_RAW",
    "MSG_BELL",
    "MSG_FRAMEBUFFER_UPDATE",
    "MSG_SERVER_CUT_TEXT",
    "MSG_SET_COLOUR_MAP_ENTRIES",
    "RFB_VERSION_3_3",
    "RFB_VERSION_3_7",
    "RFB_VERSION_3_8",
    "SECURITY_NONE",
    "Framebuffer",
    "FramebufferUpdate",
    "PixelFormat",
    "Rect",
    "RfbClient",
    "RfbError",
    "RfbStopped",
    "ServerInit",
    "parse_endpoint",
    "parse_pixel_format",
    "parse_server_init",
    "parse_version",
]
