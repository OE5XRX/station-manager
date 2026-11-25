"""Minimal PNG writer (8-bit RGB, no filter). Pure stdlib: agent and server use it."""

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode ``width*height*3`` bytes of row-major RGB as a PNG."""
    if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
        raise ValueError(f"need {width}x{height} RGB, got {len(rgb)} bytes")
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        _SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )
