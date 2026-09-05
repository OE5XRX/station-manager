# tests/test_audio_frame.py
"""§5.3 media-frame codec tests (Spec 0)."""
import struct

import pytest

from station_agent.audio import frame


def test_pack_parse_roundtrip():
    payload = b"\x01\x02\x03fake-opus"
    data = frame.pack_frame(
        stream_ref=7, seq=42, ts=1600, flags=frame.FLAG_FEC | frame.FLAG_MARKER, payload=payload
    )
    assert data[0] == 0xA5  # magic
    assert data[1] == 1  # ver
    assert len(data) == 12 + len(payload)
    mf = frame.parse_frame(data)
    assert mf.stream_ref == 7
    assert mf.seq == 42
    assert mf.ts == 1600
    assert mf.payload == payload
    assert mf.fec is True
    assert mf.marker is True
    assert mf.dtx is False


def test_header_is_little_endian_per_spec():
    data = frame.pack_frame(stream_ref=0x0102, seq=0x0304, ts=0x05060708, flags=0, payload=b"")
    # magic, ver, then LE u16 stream_ref, LE u16 seq, LE u32 ts, flags, reserved
    assert data[:12] == struct.pack("<BBHHIBB", 0xA5, 1, 0x0102, 0x0304, 0x05060708, 0, 0)


def test_flag_bits():
    d = frame.pack_frame(stream_ref=1, seq=1, ts=1, flags=frame.FLAG_DTX, payload=b"x")
    mf = frame.parse_frame(d)
    assert mf.dtx is True and mf.fec is False and mf.marker is False


def test_seq_and_ts_wrap():
    d = frame.pack_frame(stream_ref=1, seq=0x1_0001, ts=0x1_0000_0002, flags=0, payload=b"")
    mf = frame.parse_frame(d)
    assert mf.seq == 1  # wrapped at 2^16
    assert mf.ts == 2  # wrapped at 2^32
    # stream_ref must also stay in range
    d2 = frame.pack_frame(stream_ref=0x1_0005, seq=0, ts=0, flags=0, payload=b"")
    assert frame.parse_frame(d2).stream_ref == 5


def test_parse_bad_magic_fails_closed():
    d = bytearray(frame.pack_frame(stream_ref=1, seq=1, ts=1, flags=0, payload=b"y"))
    d[0] = 0x00
    with pytest.raises(frame.FrameError):
        frame.parse_frame(bytes(d))


def test_parse_unknown_version_fails_closed():
    d = bytearray(frame.pack_frame(stream_ref=1, seq=1, ts=1, flags=0, payload=b"y"))
    d[1] = 2
    with pytest.raises(frame.FrameError):
        frame.parse_frame(bytes(d))


def test_parse_short_buffer_fails_closed():
    with pytest.raises(frame.FrameError):
        frame.parse_frame(b"\xa5\x01\x00")  # < 12 bytes


def test_pack_rejects_non_bytes_payload():
    with pytest.raises(TypeError):
        frame.pack_frame(stream_ref=1, seq=1, ts=1, flags=0, payload="not-bytes")


def test_empty_payload_dtx_frame_is_valid():
    # DTX comfort-noise frames can be empty Opus packets.
    d = frame.pack_frame(stream_ref=3, seq=9, ts=0, flags=frame.FLAG_DTX, payload=b"")
    mf = frame.parse_frame(d)
    assert mf.payload == b"" and mf.dtx is True
