# tests/test_audio_rtp.py
"""Minimal RTP wrap/strip for the gst-launch <-> agent UDP boundary."""

import struct

import pytest

from station_agent.audio import rtp


def test_wrap_then_strip_roundtrip():
    payload = b"opus-packet-bytes"
    dg = rtp.wrap_rtp(payload, seq=5, ts=960, ssrc=0xDEADBEEF, pt=96)
    assert rtp.strip_rtp(dg) == payload


def test_wrap_header_shape():
    dg = rtp.wrap_rtp(b"x", seq=0x1234, ts=0x00010000, ssrc=0xAABBCCDD, pt=96, marker=True)
    v_p_x_cc, m_pt, seq, ts, ssrc = struct.unpack("!BBHII", dg[:12])
    assert v_p_x_cc == 0x80  # version 2, no padding/extension/csrc
    assert m_pt == 0x80 | 96  # marker bit + payload type
    assert seq == 0x1234
    assert ts == 0x00010000
    assert ssrc == 0xAABBCCDD
    assert dg[12:] == b"x"


def test_strip_skips_csrc_list():
    # cc=2 → two 32-bit CSRC identifiers between header and payload
    header = bytes([0x82, 96]) + struct.pack("!HII", 1, 0, 0) + b"\x00" * 8
    dg = header + b"payload"
    assert rtp.strip_rtp(dg) == b"payload"


def test_strip_skips_header_extension():
    # X bit set: after the 12-byte header comes a 4-byte ext header (profile+length),
    # then length*4 bytes of extension data.
    base = bytes([0x90, 96]) + struct.pack("!HII", 1, 0, 0)  # X=1, cc=0
    ext = struct.pack("!HH", 0xBEDE, 2) + b"\x11" * 8  # length=2 words
    dg = base + ext + b"real"
    assert rtp.strip_rtp(dg) == b"real"


def test_strip_truncated_fails_closed():
    with pytest.raises(rtp.RtpError):
        rtp.strip_rtp(b"\x80\x60\x00")  # < 12 bytes


def test_strip_wrong_version_fails_closed():
    dg = bytes([0x00, 96]) + struct.pack("!HII", 1, 0, 0) + b"p"  # version 0
    with pytest.raises(rtp.RtpError):
        rtp.strip_rtp(dg)


def test_seq_and_ssrc_wrap():
    dg = rtp.wrap_rtp(b"y", seq=0x1_0002, ts=0x1_0000_0003, ssrc=0x1_0000_0004, pt=96)
    _, _, seq, ts, ssrc = struct.unpack("!BBHII", dg[:12])
    assert seq == 2 and ts == 3 and ssrc == 4
