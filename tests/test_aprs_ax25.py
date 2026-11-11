import pytest

from apps.passive.aprs.ax25 import Ax25Error, decode_ax25, is_digipeated, to_tnc2


def _addr(call: str, last: bool, h: bool = False) -> bytes:
    name, _, ssid = call.partition("-")
    raw = bytes((ord(c) << 1) for c in name.ljust(6))
    b = 0x60 | (int(ssid or 0) << 1) | (0x80 if h else 0) | (1 if last else 0)
    return raw + bytes([b])


def _ax25(src, dst, path, info, h_bits=None):
    h_bits = h_bits or [False] * len(path)
    out = _addr(dst, False) + _addr(src, not path)
    for i, p in enumerate(path):
        out += _addr(p, i == len(path) - 1, h_bits[i])
    return out + b"\x03\xf0" + info


def test_decode_direct_frame():
    f = decode_ax25(_ax25("OE5ABC-9", "APRS", [], b"!4812.34N/01412.34E>hi"))
    assert to_tnc2(f) == "OE5ABC-9>APRS:!4812.34N/01412.34E>hi" and not is_digipeated(f.path)


def test_decode_digipeated_marks_last_used():
    f = decode_ax25(_ax25("OE5ABC", "APRS", ["OE5XBL-10", "WIDE2-1"], b">x", h_bits=[True, False]))
    assert f.path == ("OE5XBL-10*", "WIDE2-1") and is_digipeated(f.path)
    assert to_tnc2(f) == "OE5ABC>APRS,OE5XBL-10*,WIDE2-1:>x"


def test_only_last_used_digi_is_starred():
    f = decode_ax25(
        _ax25("A1B", "APRS", ["X1Y", "Z2W", "WIDE2-1"], b">x", h_bits=[True, True, False])
    )
    assert f.path == ("X1Y", "Z2W*", "WIDE2-1")


def test_decode_rejects_non_ui_and_truncated():
    good = _ax25("OE5ABC", "APRS", [], b">x")
    with pytest.raises(Ax25Error):
        decode_ax25(good[:10])
    with pytest.raises(Ax25Error):
        decode_ax25(good.replace(b"\x03\xf0", b"\x13\xf0"))
    with pytest.raises(Ax25Error):
        decode_ax25(good.replace(b"\x03\xf0", b"\x03\xcf"))
    with pytest.raises(Ax25Error):
        decode_ax25(b"")


def test_too_many_digipeaters():
    with pytest.raises(Ax25Error):
        decode_ax25(_ax25("A1B", "APRS", ["D1A"] * 9, b">x"))
