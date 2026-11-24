"""SSTV mode table — the shared contract of encoder (server) and decoder (station).

Pure stdlib, no side effects: the server imports this module.

Timings follow the published mode specifications (JL Barber, "Proposal for SSTV
mode specifications", Dayton 2000, and the MMSSTV/slowrx tables). Every line is
described as a list of ``Segment``s — a fixed tone (``hz``) or a pixel scan of
one colour channel (``channel``) — so encoder and decoder share one layout and
cannot drift apart.

Channels: ``r``/``g``/``b`` (RGB modes), ``y`` (Robot luminance), ``y0``/``y1``
(PD: luminance of the first/second of the two rows a line carries), ``v`` (R-Y)
and ``u`` (B-Y). Frequencies: sync 1200 Hz, black 1500 Hz, white 2300 Hz.
"""

from dataclasses import dataclass
from typing import Literal

SYNC_HZ = 1200
BLACK_HZ = 1500
WHITE_HZ = 2300
LEADER_HZ = 1900
VIS_ONE_HZ = 1100
VIS_ZERO_HZ = 1300

LEADER_MS = 300.0
BREAK_MS = 10.0
VIS_BIT_MS = 30.0
VIS_BITS = 10  # start + 7 data (LSB first) + even parity + stop
VIS_HEADER_S = (2 * LEADER_MS + BREAK_MS + VIS_BITS * VIS_BIT_MS) / 1000.0  # 0.91

ISS_FREQUENCY_HZ = 145_800_000
ISS_GUARD_HZ = 10_000

Color = Literal["gbr", "rgb_scottie", "yuv_robot36", "yuv_robot72", "yuv_pd"]


@dataclass(frozen=True)
class Segment:
    ms: float
    hz: int | None = None  # fixed tone
    channel: str | None = None  # pixel scan of this channel


@dataclass(frozen=True)
class Mode:
    key: str
    name: str
    vis: int
    width: int
    height: int
    color: Color
    line_ms: float
    rows_per_line: int
    sync_ms: float
    porch_ms: float
    scan_ms: float  # one RGB channel, or luminance (Robot/PD)
    separator_ms: float = 0.0
    chroma_ms: float = 0.0  # Robot only (PD chroma uses scan_ms)
    chroma_porch_ms: float = 0.0  # Robot: 1900 Hz porch after the separator
    start_ms: float = 0.0  # Scottie: one leading sync before the first line

    @property
    def lines(self) -> int:
        return self.height // self.rows_per_line


def _rgb_martin(m: Mode, line: int) -> list[Segment]:
    sep = Segment(m.separator_ms, hz=BLACK_HZ)
    return [
        Segment(m.sync_ms, hz=SYNC_HZ),
        Segment(m.porch_ms, hz=BLACK_HZ),
        Segment(m.scan_ms, channel="g"),
        sep,
        Segment(m.scan_ms, channel="b"),
        sep,
        Segment(m.scan_ms, channel="r"),
        sep,
    ]


def _rgb_scottie(m: Mode, line: int) -> list[Segment]:
    sep = Segment(m.separator_ms, hz=BLACK_HZ)
    return [
        sep,
        Segment(m.scan_ms, channel="g"),
        sep,
        Segment(m.scan_ms, channel="b"),
        Segment(m.sync_ms, hz=SYNC_HZ),
        Segment(m.porch_ms, hz=BLACK_HZ),
        Segment(m.scan_ms, channel="r"),
    ]


def _yuv_robot36(m: Mode, line: int) -> list[Segment]:
    # Even lines carry R-Y (1500 Hz separator), odd lines B-Y (2300 Hz).
    even = line % 2 == 0
    return [
        Segment(m.sync_ms, hz=SYNC_HZ),
        Segment(m.porch_ms, hz=BLACK_HZ),
        Segment(m.scan_ms, channel="y"),
        Segment(m.separator_ms, hz=BLACK_HZ if even else WHITE_HZ),
        Segment(m.chroma_porch_ms, hz=LEADER_HZ),
        Segment(m.chroma_ms, channel="v" if even else "u"),
    ]


def _yuv_robot72(m: Mode, line: int) -> list[Segment]:
    return [
        Segment(m.sync_ms, hz=SYNC_HZ),
        Segment(m.porch_ms, hz=BLACK_HZ),
        Segment(m.scan_ms, channel="y"),
        Segment(m.separator_ms, hz=BLACK_HZ),
        Segment(m.chroma_porch_ms, hz=LEADER_HZ),
        Segment(m.chroma_ms, channel="v"),
        Segment(m.separator_ms, hz=WHITE_HZ),
        Segment(m.chroma_porch_ms, hz=LEADER_HZ),
        Segment(m.chroma_ms, channel="u"),
    ]


def _yuv_pd(m: Mode, line: int) -> list[Segment]:
    return [
        Segment(m.sync_ms, hz=SYNC_HZ),
        Segment(m.porch_ms, hz=BLACK_HZ),
        Segment(m.scan_ms, channel="y0"),
        Segment(m.scan_ms, channel="v"),
        Segment(m.scan_ms, channel="u"),
        Segment(m.scan_ms, channel="y1"),
    ]


_LAYOUT = {
    "gbr": _rgb_martin,
    "rgb_scottie": _rgb_scottie,
    "yuv_robot36": _yuv_robot36,
    "yuv_robot72": _yuv_robot72,
    "yuv_pd": _yuv_pd,
}


def line_segments(mode: Mode, line: int) -> list[Segment]:
    """Tone/scan layout of picture line ``line`` (0-based, not picture row)."""
    return _LAYOUT[mode.color](mode, line)


def start_segments(mode: Mode) -> list[Segment]:
    """Tones sent once between the VIS stop bit and the first line."""
    return [Segment(mode.start_ms, hz=SYNC_HZ)] if mode.start_ms else []


def sync_offset_ms(mode: Mode) -> float:
    """Start of the line's sync pulse, measured from the start of the line."""
    offset = 0.0
    for seg in line_segments(mode, 0):
        if seg.hz == SYNC_HZ:
            return offset
        offset += seg.ms
    raise AssertionError(f"mode {mode.key} has no sync")  # pragma: no cover


def duration_s(mode: Mode) -> float:
    """Picture duration (leading sync + all lines), excluding leader and VIS."""
    return (mode.start_ms + mode.lines * mode.line_ms) / 1000.0


def _martin(key, name, vis, scan_ms):
    sync, porch, sep = 4.862, 0.572, 0.572
    return Mode(
        key=key, name=name, vis=vis, width=320, height=256, color="gbr",
        line_ms=sync + porch + 3 * (scan_ms + sep), rows_per_line=1,
        sync_ms=sync, porch_ms=porch, scan_ms=scan_ms, separator_ms=sep,
    )  # fmt: skip


def _scottie(key, name, vis, scan_ms):
    sync, porch, sep = 9.0, 1.5, 1.5
    return Mode(
        key=key, name=name, vis=vis, width=320, height=256, color="rgb_scottie",
        line_ms=2 * sep + sync + porch + 3 * scan_ms, rows_per_line=1,
        sync_ms=sync, porch_ms=porch, scan_ms=scan_ms, separator_ms=sep,
        start_ms=sync,
    )  # fmt: skip


def _pd(key, name, vis, size, scan_ms):
    sync, porch = 20.0, 2.08
    return Mode(
        key=key, name=name, vis=vis, width=size[0], height=size[1], color="yuv_pd",
        line_ms=sync + porch + 4 * scan_ms, rows_per_line=2,
        sync_ms=sync, porch_ms=porch, scan_ms=scan_ms,
    )  # fmt: skip


_ROBOT36 = Mode(
    key="robot36", name="Robot 36", vis=8, width=320, height=240, color="yuv_robot36",
    line_ms=9.0 + 3.0 + 88.0 + 4.5 + 1.5 + 44.0, rows_per_line=1,
    sync_ms=9.0, porch_ms=3.0, scan_ms=88.0, separator_ms=4.5,
    chroma_ms=44.0, chroma_porch_ms=1.5,
)  # fmt: skip

_ROBOT72 = Mode(
    key="robot72", name="Robot 72", vis=12, width=320, height=240, color="yuv_robot72",
    line_ms=9.0 + 3.0 + 138.0 + 2 * (4.5 + 1.5 + 69.0), rows_per_line=1,
    sync_ms=9.0, porch_ms=3.0, scan_ms=138.0, separator_ms=4.5,
    chroma_ms=69.0, chroma_porch_ms=1.5,
)  # fmt: skip

MODES: dict[str, Mode] = {
    m.key: m
    for m in (
        _martin("martin1", "Martin M1", 44, 146.432),
        _martin("martin2", "Martin M2", 40, 73.216),
        _scottie("scottie1", "Scottie S1", 60, 138.240),
        _scottie("scottie2", "Scottie S2", 56, 88.064),
        _scottie("scottiedx", "Scottie DX", 76, 345.600),
        _ROBOT36,
        _ROBOT72,
        _pd("pd90", "PD 90", 99, (320, 256), 170.240),
        _pd("pd120", "PD 120", 95, (640, 496), 121.600),
        _pd("pd180", "PD 180", 96, (640, 496), 183.040),
    )
}

BY_VIS: dict[int, Mode] = {m.vis: m for m in MODES.values()}
