"""§5.3 media-frame codec (Spec 0) — pure, no I/O.

Binary media frames on both hops carry a fixed little-endian 12-byte header followed by
exactly one Opus packet (a 20 ms frame). ``stream_ref`` is the numeric handle for a
``stream_id`` established in ``advertise``/``subscribe`` so the string id is not sent per
frame.

Layout (little-endian):
    0  u8   magic       = 0xA5
    1  u8   ver         = 1
    2  u16  stream_ref
    4  u16  seq         per-stream, wraps 2^16
    6  u32  ts          RTP-style timestamp in samples at the stream rate
    10 u8   flags       bit0 FEC-present, bit1 DTX/comfort, bit2 marker(talk-onset)
    11 u8   reserved    = 0
    12 …    payload     one Opus packet
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = 0xA5
VERSION = 1
HEADER_LEN = 12

FLAG_FEC = 0x01
FLAG_DTX = 0x02
FLAG_MARKER = 0x04

# magic, ver, stream_ref(u16), seq(u16), ts(u32), flags(u8), reserved(u8)
_HEADER = struct.Struct("<BBHHIBB")

_U16 = 0xFFFF
_U32 = 0xFFFFFFFF


class FrameError(ValueError):
    """A media frame failed to parse (bad magic/version/length). Fail closed."""


@dataclass(frozen=True)
class MediaFrame:
    stream_ref: int
    seq: int
    ts: int
    flags: int
    payload: bytes

    @property
    def fec(self) -> bool:
        return bool(self.flags & FLAG_FEC)

    @property
    def dtx(self) -> bool:
        return bool(self.flags & FLAG_DTX)

    @property
    def marker(self) -> bool:
        return bool(self.flags & FLAG_MARKER)


def pack_frame(*, stream_ref: int, seq: int, ts: int, flags: int, payload: bytes) -> bytes:
    """Serialize one §5.3 media frame.

    ``stream_ref``/``seq`` are masked to 16 bits and ``ts`` to 32 bits (they wrap by
    design), so a caller that keeps a monotonically increasing counter never overflows the
    struct. ``payload`` must be a bytes-like object (an empty payload is valid for DTX
    comfort frames).
    """
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    header = _HEADER.pack(
        MAGIC,
        VERSION,
        stream_ref & _U16,
        seq & _U16,
        ts & _U32,
        flags & 0xFF,
        0,
    )
    return header + bytes(payload)


def parse_frame(data: bytes) -> MediaFrame:
    """Parse one §5.3 media frame or raise :class:`FrameError` (fail closed)."""
    if len(data) < HEADER_LEN:
        raise FrameError(f"frame too short: {len(data)} < {HEADER_LEN}")
    magic, ver, stream_ref, seq, ts, flags, _reserved = _HEADER.unpack_from(data)
    if magic != MAGIC:
        raise FrameError(f"bad magic 0x{magic:02x}")
    if ver != VERSION:
        raise FrameError(f"unsupported frame version {ver}")
    return MediaFrame(
        stream_ref=stream_ref,
        seq=seq,
        ts=ts,
        flags=flags,
        payload=bytes(data[HEADER_LEN:]),
    )
