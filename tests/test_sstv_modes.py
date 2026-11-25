"""SSTV mode table (shared contract, station_agent/sstv/modes.py)."""

import pytest

from station_agent.sstv.modes import (
    BY_VIS,
    MODES,
    VIS_HEADER_S,
    duration_s,
    line_segments,
    sync_offset_ms,
)


@pytest.mark.parametrize(
    "key,vis,w,h,dur",
    [
        ("martin1", 44, 320, 256, 114.3),
        ("martin2", 40, 320, 256, 58.1),
        ("scottie1", 60, 320, 256, 109.6),
        ("scottie2", 56, 320, 256, 71.1),
        ("scottiedx", 76, 320, 256, 268.9),
        ("robot36", 8, 320, 240, 36.0),
        ("robot72", 12, 320, 240, 72.0),
        ("pd90", 99, 320, 256, 90.0),
        ("pd120", 95, 640, 496, 126.1),
        ("pd180", 96, 640, 496, 187.1),
    ],
)
def test_mode_table(key, vis, w, h, dur):
    m = MODES[key]
    assert m.key == key
    assert (m.vis, m.width, m.height) == (vis, w, h)
    assert duration_s(m) == pytest.approx(dur, abs=0.5)
    assert BY_VIS[vis] is m


def test_exactly_the_spec_modes():
    assert len(MODES) == 10
    assert len(BY_VIS) == 10


def test_vis_header_length():
    assert VIS_HEADER_S == pytest.approx(0.91)


def test_vis_codes_fit_seven_bits():
    for m in MODES.values():
        assert 0 <= m.vis < 128


@pytest.mark.parametrize("key", sorted(MODES))
def test_line_segments_sum_to_line_ms(key):
    m = MODES[key]
    for line in range(m.height // m.rows_per_line):
        assert sum(s.ms for s in line_segments(m, line)) == pytest.approx(m.line_ms)


@pytest.mark.parametrize("key", sorted(MODES))
def test_each_line_has_exactly_one_sync_at_the_sync_offset(key):
    m = MODES[key]
    for line in (0, 1):
        segs = line_segments(m, line)
        syncs = [s for s in segs if s.hz == 1200]
        assert len(syncs) == 1
        assert syncs[0].ms == pytest.approx(m.sync_ms)
        offset = 0.0
        for s in segs:
            if s is syncs[0]:
                break
            offset += s.ms
        assert offset == pytest.approx(sync_offset_ms(m))


@pytest.mark.parametrize(
    "key,channels",
    [
        ("martin1", [("g", "b", "r")] * 2),
        ("scottie1", [("g", "b", "r")] * 2),
        ("robot36", [("y", "v"), ("y", "u")]),
        ("robot72", [("y", "v", "u")] * 2),
        ("pd120", [("y0", "v", "u", "y1")] * 2),
    ],
)
def test_channel_order(key, channels):
    m = MODES[key]
    for line, expected in enumerate(channels):
        got = tuple(s.channel for s in line_segments(m, line) if s.channel)
        assert got == expected


def test_robot36_separator_marks_chroma():
    m = MODES["robot36"]
    seps = [[s.hz for s in line_segments(m, line) if s.hz in (1500, 2300)] for line in (0, 1)]
    # sync porch (1500) + separator: 1500 on R-Y lines, 2300 on B-Y lines
    assert seps[0] == [1500, 1500]
    assert seps[1] == [1500, 2300]


def test_scottie_has_leading_sync():
    assert MODES["scottie1"].start_ms == pytest.approx(9.0)
    assert MODES["martin1"].start_ms == 0


@pytest.mark.parametrize(
    "key,line_ms",
    [
        ("martin1", 446.446),
        ("martin2", 226.798),
        ("scottie1", 428.22),
        ("scottie2", 277.692),
        ("scottiedx", 1050.3),
        ("robot36", 150.0),
        ("robot72", 300.0),
        ("pd90", 703.04),
        ("pd120", 508.48),
        ("pd180", 754.24),
    ],
)
def test_line_timing_matches_published_tables(key, line_ms):
    # Encoder and decoder share this table, so a round trip cannot catch a
    # wrong timing; pin it to the Dayton paper / MMSSTV values instead.
    assert MODES[key].line_ms == pytest.approx(line_ms, abs=1e-3)
