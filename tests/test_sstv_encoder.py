"""SSTV encoder + PNG writer (station_agent/sstv/encoder.py, png.py)."""

import io
import math
import struct
import time

import pytest
from PIL import Image

from station_agent.sstv.encoder import encode
from station_agent.sstv.modes import MODES, VIS_HEADER_S, duration_s
from station_agent.sstv.png import encode_png

SR = 8000


def _picture(w, h):
    out = bytearray()
    for y in range(h):
        for x in range(w):
            out += bytes(((x * 255) // max(w - 1, 1), (y * 255) // max(h - 1, 1), (x ^ y) & 0xFF))
    return bytes(out)


def _samples(pcm):
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)


def _goertzel_peak(samples, freqs):
    """Return the frequency in ``freqs`` with the most energy in ``samples``."""

    def power(f):
        coeff = 2 * math.cos(2 * math.pi * f / SR)
        s1 = s2 = 0.0
        for x in samples:
            s1, s2 = x + coeff * s1 - s2, s1
        return s1 * s1 + s2 * s2 - coeff * s1 * s2

    return max(freqs, key=power)


@pytest.mark.parametrize("key", sorted(MODES))
def test_length_matches_mode_duration(key):
    m = MODES[key]
    pcm = encode(m, _picture(m.width, m.height))
    assert len(pcm) % 2 == 0
    assert len(pcm) / 2 / SR == pytest.approx(VIS_HEADER_S + duration_s(m), abs=0.05)


def test_peak_amplitude_is_point_nine_full_scale():
    m = MODES["robot36"]
    s = _samples(encode(m, _picture(m.width, m.height)))
    assert max(abs(x) for x in s) == pytest.approx(0.9 * 32767, rel=0.01)


def test_phase_continuous():
    # A phase jump shows up as a sample-to-sample step far larger than the
    # largest step of a continuous 2300 Hz tone at 0.9 FS.
    m = MODES["martin2"]
    s = _samples(encode(m, _picture(m.width, m.height)))
    max_step = 0.9 * 32767 * 2 * math.pi * 2300 / SR
    assert max(abs(b - a) for a, b in zip(s, s[1:], strict=False)) <= max_step * 1.05


@pytest.mark.parametrize("bad", [b"", b"\x00" * 10, b"\x00" * (320 * 256 * 3 + 1)])
def test_wrong_rgb_size_raises_valueerror(bad):
    with pytest.raises(ValueError):
        encode(MODES["martin1"], bad)


def test_png_roundtrip():
    w, h = 37, 11
    rgb = _picture(w, h)
    img = Image.open(io.BytesIO(encode_png(w, h, rgb)))
    assert img.mode == "RGB"
    assert img.size == (w, h)
    assert img.tobytes() == rgb


def test_png_wrong_size_raises_valueerror():
    with pytest.raises(ValueError):
        encode_png(2, 2, b"\x00" * 11)


@pytest.mark.parametrize("key", sorted(MODES))
def test_vis_tone_sequence(key):
    m = MODES[key]
    s = _samples(encode(m, _picture(m.width, m.height)))

    def tone(start_ms, length_ms, freqs):
        a = int(start_ms * SR / 1000)
        b = int((start_ms + length_ms) * SR / 1000)
        return _goertzel_peak(s[a:b], freqs)

    leader = (1100, 1200, 1300, 1500, 1900, 2300)
    assert tone(10, 280, leader) == 1900
    assert tone(301, 8, leader) == 1200
    assert tone(320, 280, leader) == 1900
    bits = []
    for i in range(10):
        bits.append(tone(610 + i * 30 + 3, 24, (1100, 1200, 1300)))
    assert bits[0] == 1200 and bits[9] == 1200
    data = [1 if f == 1100 else 0 for f in bits[1:9]]
    assert all(f in (1100, 1300) for f in bits[1:9])
    assert sum(bit << i for i, bit in enumerate(data[:7])) == m.vis
    assert sum(data) % 2 == 0  # even parity over 7 data bits + parity bit


def test_encode_pd180_under_3s():
    m = MODES["pd180"]
    rgb = _picture(m.width, m.height)
    t0 = time.perf_counter()
    encode(m, rgb)
    assert time.perf_counter() - t0 < 3.0
