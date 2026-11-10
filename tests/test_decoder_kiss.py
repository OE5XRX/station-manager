from station_agent.decoders import FEATURE
from station_agent.decoders.kiss import KissDeframer


def test_feature_name():
    assert FEATURE == "decoders"


def test_kiss_unescapes_and_splits():
    d = KissDeframer()
    assert d.feed(b"\xc0\x00ab\xdb\xdccd\xc0\xc0\x00x") == [b"ab\xc0cd"]
    assert d.feed(b"y\xdb\xdd\xc0") == [b"xy\xdb"]


def test_kiss_ignores_non_data_and_port1():
    assert KissDeframer().feed(b"\xc0\x01zz\xc0\xc0\x10zz\xc0") == []


def test_kiss_drops_oversized():
    assert KissDeframer().feed(b"\xc0\x00" + b"a" * 1025 + b"\xc0") == []


def test_kiss_keeps_escape_heavy_valid_frame():
    payload = b"\xc0\xdb" * 300  # 600 decoded bytes, 1200 escaped
    raw = payload.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc")
    assert KissDeframer().feed(b"\xc0\x00" + raw + b"\xc0") == [payload]


def test_kiss_recovers_after_oversized():
    d = KissDeframer()
    assert d.feed(b"\xc0\x00" + b"a" * 2000 + b"\xc0\x00ok\xc0") == [b"ok"]


def test_kiss_oversized_across_calls_does_not_grow_unbounded():
    d = KissDeframer()
    for _ in range(10):
        d.feed(b"a" * 1000)
    assert len(d._buf) <= 2100
