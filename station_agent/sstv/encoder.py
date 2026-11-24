"""SSTV encoder: RGB picture -> s16le mono PCM (leader + VIS + picture).

Pure Python, no numpy: the server synthesises TX audio with it. One phase
accumulator over a precomputed sine table keeps the signal phase-continuous;
segment boundaries are placed on the exact (fractional) timeline and rounded
per segment, so timing never drifts over a picture.
"""

import math
from array import array
from sys import byteorder

from .modes import (
    BLACK_HZ,
    BREAK_MS,
    LEADER_HZ,
    LEADER_MS,
    SYNC_HZ,
    VIS_BIT_MS,
    VIS_ONE_HZ,
    VIS_ZERO_HZ,
    WHITE_HZ,
    Mode,
    line_segments,
    start_segments,
)

AMPLITUDE = 0.9
_TABLE_BITS = 12
_SHIFT = 32 - _TABLE_BITS
_MASK32 = 0xFFFFFFFF
_TABLE = [
    round(AMPLITUDE * 32767 * math.sin(2 * math.pi * i / (1 << _TABLE_BITS)))
    for i in range(1 << _TABLE_BITS)
]
_HZ_PER_LEVEL = (WHITE_HZ - BLACK_HZ) / 255.0


def _vis_tones(vis: int) -> list[tuple[float, float]]:
    data = [(vis >> i) & 1 for i in range(7)]
    data.append(sum(data) & 1)  # even parity
    tones = [(LEADER_HZ, LEADER_MS), (SYNC_HZ, BREAK_MS), (LEADER_HZ, LEADER_MS)]
    tones.append((SYNC_HZ, VIS_BIT_MS))
    tones += [(VIS_ONE_HZ if bit else VIS_ZERO_HZ, VIS_BIT_MS) for bit in data]
    tones.append((SYNC_HZ, VIS_BIT_MS))
    return tones


def _yuv(r: int, g: int, b: int) -> tuple[float, float, float]:
    """BT.601 studio range, as MMSSTV/slowrx (Y 16..235, U/V 16..240)."""
    y = 16.0 + (65.738 * r + 129.057 * g + 25.064 * b) / 256.0
    v = 128.0 + (112.439 * r - 94.154 * g - 18.285 * b) / 256.0
    u = 128.0 + (-37.945 * r - 74.494 * g + 112.439 * b) / 256.0
    return y, u, v


def _channels(mode: Mode, rgb: bytes, line: int) -> dict[str, list[float]]:
    """Pixel levels (0..255) of every channel line ``line`` transmits."""
    w = mode.width
    stride = w * 3

    def row(index: int) -> bytes:
        return rgb[index * stride : (index + 1) * stride]

    if mode.color in ("gbr", "rgb_scottie"):
        px = row(line)
        return {"r": list(px[0::3]), "g": list(px[1::3]), "b": list(px[2::3])}

    if mode.color == "yuv_pd":
        rows = (2 * line, 2 * line + 1)
    elif mode.color == "yuv_robot36":
        # Chroma alternates per line; each line pair shares it, so average it.
        rows = (line - line % 2, line - line % 2 + 1)
    else:  # yuv_robot72
        rows = (line,)
    yuv = [[_yuv(*px[i : i + 3]) for i in range(0, stride, 3)] for px in map(row, rows)]
    n = len(rows)
    out = {
        "u": [sum(p[x][1] for p in yuv) / n for x in range(w)],
        "v": [sum(p[x][2] for p in yuv) / n for x in range(w)],
    }
    if mode.color == "yuv_pd":
        out["y0"] = [p[0] for p in yuv[0]]
        out["y1"] = [p[0] for p in yuv[1]]
    else:
        out["y"] = [p[0] for p in yuv[line - rows[0]]]
    return out


def _tones(mode: Mode, rgb: bytes):
    """Yield (frequency_hz, duration_ms) for the whole transmission."""
    yield from _vis_tones(mode.vis)
    for seg in start_segments(mode):
        yield seg.hz, seg.ms
    for line in range(mode.lines):
        levels = _channels(mode, rgb, line)
        for seg in line_segments(mode, line):
            if seg.channel is None:
                yield seg.hz, seg.ms
                continue
            px_ms = seg.ms / mode.width
            for v in levels[seg.channel]:
                v = min(max(v, 0.0), 255.0)
                yield BLACK_HZ + v * _HZ_PER_LEVEL, px_ms


def encode(mode: Mode, rgb: bytes, *, sample_rate: int = 8000) -> bytes:
    """Encode a ``width*height*3`` byte RGB picture (row-major) as s16le mono PCM."""
    if len(rgb) != mode.width * mode.height * 3:
        raise ValueError(f"{mode.key} needs {mode.width}x{mode.height} RGB, got {len(rgb)} bytes")
    table = _TABLE
    out = array("h")
    phase = 0
    t_end = 0.0  # exact segment end, in samples
    n = 0  # samples emitted so far
    scale = (1 << 32) / sample_rate
    ms_to_samples = sample_rate / 1000.0
    for hz, ms in _tones(mode, rgb):
        t_end += ms * ms_to_samples
        count = round(t_end) - n
        if count <= 0:
            continue
        inc = round(hz * scale)
        stop = phase + inc * count
        out.extend([table[(p & _MASK32) >> _SHIFT] for p in range(phase + inc, stop + 1, inc)])
        phase = stop & _MASK32
        n += count
    if byteorder != "little":  # pragma: no cover
        out.byteswap()
    return out.tobytes()
