"""Minimal RTP (RFC 3550) wrap/strip for the gst-launch <-> agent UDP boundary — pure.

Without a GStreamer ``appsink`` (which needs python-gi, a package the A-image does not
ship), a UDP datagram is the cheapest self-delimiting boundary that yields exactly one
Opus packet per read. The bridge pipelines therefore RTP-payload Opus over UDP loopback:

    RX:  … ! opusenc ! rtpopuspay ! udpsink   → agent ``strip_rtp`` → §5.3 media frame
    TX:  agent ``wrap_rtp`` → udpsrc ! rtpopusdepay ! opusdec ! …

We only care about the Opus payload; the RTP seq/ts here are the transport's, distinct
from the §5.3 header's per-stream seq/ts. For TX injection we emit a minimal, valid header
(no padding/extension/CSRC) that ``rtpopusdepay`` accepts. The RTP timestamp uses the
48 kHz Opus RTP clock (RFC 7587), i.e. 960 ticks per 20 ms frame regardless of the media
sample rate; the caller owns advancing seq/ts.
"""

from __future__ import annotations

import struct

RTP_VERSION = 2
_FIXED = struct.Struct("!BBHII")  # V/P/X/CC, M/PT, seq, timestamp, SSRC
_FIXED_LEN = 12

_U16 = 0xFFFF
_U32 = 0xFFFFFFFF


class RtpError(ValueError):
    """An RTP datagram failed to parse. Fail closed."""


def wrap_rtp(
    payload: bytes,
    *,
    seq: int,
    ts: int,
    ssrc: int,
    pt: int = 96,
    marker: bool = False,
) -> bytes:
    """Build a minimal RTP/Opus datagram for TX injection.

    No padding, no extension, no CSRC. ``seq``/``ssrc`` wrap to their field widths so a
    monotonic caller counter never overflows.
    """
    first = RTP_VERSION << 6  # P=0, X=0, CC=0
    second = ((0x80 if marker else 0x00) | (pt & 0x7F))
    header = _FIXED.pack(first, second, seq & _U16, ts & _U32, ssrc & _U32)
    return header + bytes(payload)


def strip_rtp(datagram: bytes) -> bytes:
    """Return the Opus payload from an RTP datagram, or raise :class:`RtpError`.

    Handles the CSRC list (``CC`` count) and an optional header extension (``X`` bit) so a
    payloader that inserts either does not corrupt the payload boundary.
    """
    if len(datagram) < _FIXED_LEN:
        raise RtpError(f"RTP datagram too short: {len(datagram)}")
    first, _second, _seq, _ts, _ssrc = _FIXED.unpack_from(datagram)
    if (first >> 6) != RTP_VERSION:
        raise RtpError(f"bad RTP version {first >> 6}")
    cc = first & 0x0F
    has_ext = bool(first & 0x10)
    offset = _FIXED_LEN + 4 * cc
    if has_ext:
        if len(datagram) < offset + 4:
            raise RtpError("RTP extension header truncated")
        _profile, ext_words = struct.unpack_from("!HH", datagram, offset)
        offset += 4 + 4 * ext_words
    if offset > len(datagram):
        raise RtpError("RTP header longer than datagram")
    return bytes(datagram[offset:])
