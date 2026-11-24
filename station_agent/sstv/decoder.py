"""Streaming SSTV decoder (numpy). Station only — server code never imports this.

Pipeline, per block of audio:

1. **FM discriminator.** Mix to complex baseband around 1700 Hz (the middle of
   the 1100..2300 Hz SSTV band), low-pass FIR, then the phase step between
   consecutive samples is the instantaneous frequency. Output is aligned to
   the input sample index (FIR group delay removed).
2. **Leader/VIS detection** — always running, also during a picture (a new
   VIS ends the running picture). A VIS is accepted only with *both*
   300 ms 1900 Hz leader halves, the 1200 Hz break between them, a 1200 Hz
   start and stop bit and seven data bits with correct even parity, all at
   the nominal 30 ms bit timing. The leader also yields the receiver's
   frequency offset, which is removed from everything that follows.
3. **Line decoding** (picture). Each line's 1200 Hz sync pulse is searched in
   a small window around its predicted position (matched filter on "below
   1350 Hz"). A linear regression over the recent good syncs predicts the next
   one and places the pixels — this tracks sample-rate offsets (slant).
   Pixel values are the mean frequency over each pixel's time span.
4. **Loss handling.** Lines with a missing sync are held back; a later good
   sync confirms them. After ~3 s without a good sync the picture ends
   incomplete with only the confirmed rows.
"""

import math
from dataclasses import dataclass

import numpy as np

from .modes import (
    BLACK_HZ,
    BREAK_MS,
    BY_VIS,
    LEADER_HZ,
    LEADER_MS,
    SYNC_HZ,
    VIS_BIT_MS,
    VIS_BITS,
    WHITE_HZ,
    Mode,
    line_segments,
    sync_offset_ms,
)


@dataclass(frozen=True)
class Start:
    mode: Mode


@dataclass(frozen=True)
class Row:
    index: int
    rgb: bytes


@dataclass(frozen=True)
class End:
    complete: bool
    rows: int


Event = Start | Row | End

_MIX_HZ = 1700.0
_LP_CUTOFF_HZ = 700.0
_LP_TAPS = 97
_BLOCK_MS = 100.0  # demodulate/decode in blocks of at least this much audio

_SMOOTH_MS = 5.0  # tone detection smoothing (VIS)
_LEADER_TOL_HZ = 100.0
_LEADER_MIN_FRAC = 0.7
_BREAK_TOL_HZ = 120.0
_BIT_TOL_HZ = 60.0  # start/stop bit
_BIT_MIN_SPLIT_HZ = 25.0  # data bit: at least this far from 1200 Hz ...
_BIT_MAX_SPLIT_HZ = 160.0  # ... and no further than this
_MAX_OFFSET_HZ = 100.0

_SYNC_THRESHOLD_HZ = (SYNC_HZ + BLACK_HZ) / 2  # 1350
_SYNC_MIN_QUALITY = 0.7  # a one-sided frequency step scores only ~0.5
_WIDE_SYNC_WINDOW_MS = 10.0
_FIT_POINTS = 32
_FIT_MIN_POINTS = 4
_LOSS_MS = 3000.0
_LOSS_MIN_LINES = 5
_RELOCK_LINES = 3

_LEVEL_PER_HZ = 255.0 / (WHITE_HZ - BLACK_HZ)


def _lowpass(cutoff_hz: float, sample_rate: int, taps: int) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    fc = cutoff_hz / sample_rate
    h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(taps)
    return h / h.sum()


def _smooth3(x: np.ndarray) -> np.ndarray:
    """[1 2 1]/4 horizontal smoothing (edges replicated)."""
    p = np.concatenate([x[:1], x, x[-1:]])
    return (p[:-2] + 2 * p[1:-1] + p[2:]) / 4


def _yuv_to_rgb(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Inverse of the encoder's BT.601 studio-range conversion -> (w, 3) uint8.

    Chroma is smoothed first: it amplifies noise ~2x into RGB, and SSTV sends
    it at (at most) luminance resolution anyway."""
    y = 1.164 * (y - 16.0)
    u = _smooth3(u) - 128.0
    v = _smooth3(v) - 128.0
    rgb = np.stack([y + 1.596 * v, y - 0.813 * v - 0.391 * u, y + 2.018 * u], axis=1)
    return np.clip(np.rint(rgb), 0, 255).astype(np.uint8)


def _rgb(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(np.stack([r, g, b], axis=1)), 0, 255).astype(np.uint8)


class StreamDecoder:
    """Feed s16 mono audio; get ``Start``/``Row``/``End`` events back."""

    def __init__(self, sample_rate: int = 8000):
        if sample_rate < 2 * WHITE_HZ + 1000:
            raise ValueError(f"sample rate {sample_rate} too low for SSTV")
        self.sample_rate = sample_rate
        self._spm = sample_rate / 1000.0  # samples per millisecond
        self._block = int(_BLOCK_MS * self._spm)
        self._taps = _lowpass(_LP_CUTOFF_HZ, sample_rate, _LP_TAPS)
        self._smooth = int(_SMOOTH_MS * self._spm) | 1  # odd => centred
        self._reset()

    # -- public API -------------------------------------------------------

    def feed(self, samples) -> list[Event]:
        """Decode a chunk of int16 samples; returns the events it completed."""
        chunk = np.asarray(samples)
        if chunk.size:
            self._pending.append(chunk.astype(np.float64).ravel() / 32768.0)
            self._pending_n += chunk.size
        if self._pending_n < self._block:
            return []
        return self._process()

    def flush(self) -> list[Event]:
        """End of stream: decode what is buffered, end a running picture
        (incomplete, confirmed rows only) and reset to idle."""
        # Drain the FIR delay line so the last input samples reach the buffer.
        self._pending.append(np.zeros((_LP_TAPS - 1) // 2))
        events = self._process()
        if self._mode is not None:
            # Decode the lines whose audio is complete: pad with black so the
            # sync search window and the FIR tail do not need audio after EOF.
            self._final_end = self._n_in - (_LP_TAPS - 1) // 2
            pad = math.ceil(2 * self._line_len + self._ms(50))
            self._f = np.concatenate([self._f, np.full(pad, BLACK_HZ + self._offset)])
            while self._mode is not None and self._decode_line(events):
                pass
        if self._mode is not None:
            events.append(End(complete=False, rows=self._emitted))
        self._reset()
        return events

    # -- state --------------------------------------------------------------

    def _reset(self) -> None:
        self._hist = np.zeros(_LP_TAPS - 1, dtype=np.complex128)
        self._mix_phase = 0.0
        self._prev_z = complex(0.0)
        self._pending: list[np.ndarray] = []
        self._pending_n = 0
        self._n_in = 0
        self._f = np.zeros(0)
        self._f0 = -((_LP_TAPS - 1) // 2)  # absolute index of self._f[0]
        self._vis_cursor = self._f0
        self._mode: Mode | None = None
        self._final_end: int | None = None
        self._barrier: int | None = None  # a new VIS: decode only lines ending before it
        self._keep_from: float = self._f0  # oldest audio the running picture still needs

    @property
    def _end(self) -> int:
        return self._f0 + len(self._f)

    def _seg(self, a: int, b: int) -> np.ndarray:
        if a < self._f0 or b > self._end:
            # A negative slice start would wrap silently: this is a bug, not noise.
            raise RuntimeError(f"audio [{a}, {b}) outside buffer [{self._f0}, {self._end})")
        return self._f[a - self._f0 : b - self._f0]

    def _trim(self, keep_from: int) -> None:
        if keep_from > self._f0:
            self._f = self._f[keep_from - self._f0 :]
            self._f0 = keep_from

    # -- demodulation -------------------------------------------------------

    def _process(self) -> list[Event]:
        if self._pending:
            x = np.concatenate(self._pending)
            self._pending = []
            self._pending_n = 0
            self._demodulate(x)
        events: list[Event] = []
        while True:
            found = self._scan_vis()
            if found is not None:
                mode, p, offset = found
                if self._mode is not None:
                    # Lines that end before the new leader still belong to the
                    # running picture; then it ends (incomplete if cut short).
                    self._barrier = p - self._ms(2 * LEADER_MS + BREAK_MS)
                    while self._mode is not None and self._decode_line(events):
                        pass
                    self._barrier = None
                    if self._mode is not None:
                        self._finish(events, complete_ok=False)
                self._start_picture(mode, p, offset)
                events.append(Start(mode))
            while self._mode is not None and self._decode_line(events):
                pass
            if found is None:
                break
        self._trim_buffer()
        return events

    def _demodulate(self, x: np.ndarray) -> None:
        step = 2 * math.pi * _MIX_HZ / self.sample_rate
        phase = self._mix_phase + step * np.arange(len(x))
        self._mix_phase = (self._mix_phase + step * len(x)) % (2 * math.pi)
        full = np.concatenate([self._hist, x * np.exp(-1j * phase)])
        self._hist = full[-(_LP_TAPS - 1) :]
        z = np.convolve(full, self._taps, mode="valid")
        prev = np.concatenate([[self._prev_z], z[:-1]])
        self._prev_z = z[-1]
        hz = _MIX_HZ + np.angle(z * np.conj(prev)) * self.sample_rate / (2 * math.pi)
        self._f = np.concatenate([self._f, hz])
        self._n_in += len(x)

    # -- VIS ----------------------------------------------------------------

    def _ms(self, ms: float) -> int:
        return round(ms * self._spm)

    def _vis_span(self) -> tuple[int, int, int]:
        h = self._smooth // 2
        pre = self._ms(2 * LEADER_MS + BREAK_MS - 5)  # p - pre .. p: leaders + break
        post = self._ms(VIS_BITS * VIS_BIT_MS + 5)  # p .. p + post: the VIS bits
        return h, pre, post

    def _scan_vis(self):
        """Search the not yet scanned audio for a VIS -> (mode, p, offset) or None."""
        h, pre, post = self._vis_span()
        p_lo = max(self._f0 + pre + h, self._vis_cursor)
        p_hi = self._end - post - h
        if p_hi <= p_lo:
            return None
        base = p_lo - pre  # absolute index of fs[0]
        f = self._seg(base - h, p_hi + post + h)
        c = np.concatenate([[0.0], np.cumsum(f)])
        fs = (c[self._smooth :] - c[: -self._smooth]) / self._smooth
        ind = np.concatenate([[0], np.cumsum(np.abs(fs - LEADER_HZ) < _LEADER_TOL_HZ)])

        def frac(a: int, b: int) -> float:  # leader fraction over fs[a:b]
            return (ind[b] - ind[a]) / (b - a)

        j = np.arange(pre, p_hi - base)
        mid = (LEADER_HZ + SYNC_HZ) / 2
        lead_a, lead_b = self._ms(LEADER_MS - 5), self._ms(5)
        crossing = (fs[j - 1] >= mid) & (fs[j] < mid)
        leader2 = (ind[j - lead_b] - ind[j - lead_a]) / (lead_a - lead_b) >= _LEADER_MIN_FRAC
        for jj in j[crossing & leader2]:
            found = self._check_vis(int(jj), base, fs, frac)
            if found is not None:
                # Past both the candidate and the refined edge: never re-detect it.
                self._vis_cursor = max(base + int(jj), found[1]) + 1
                return found
        self._vis_cursor = p_hi
        return None

    def _trim_buffer(self) -> None:
        """Drop audio that neither the VIS scan nor the running picture needs."""
        h, pre, _ = self._vis_span()
        keep = self._vis_cursor - pre - h - 1
        if self._mode is not None:
            keep = min(keep, math.floor(self._keep_from) - 4)
        self._trim(keep)

    def _check_vis(self, j: int, base: int, fs: np.ndarray, frac):
        """Validate a candidate start-bit edge at fs index ``j``."""
        ms = self._ms
        leader2 = fs[j - ms(LEADER_MS - 10) : j - ms(10)]
        offset = float(np.median(leader2)) - LEADER_HZ
        if abs(offset) > _MAX_OFFSET_HZ:
            return None
        l1_end = j - ms(LEADER_MS + BREAK_MS + 5)
        l1_start = j - ms(2 * LEADER_MS + BREAK_MS - 5)
        if frac(l1_start, l1_end) < _LEADER_MIN_FRAC:
            return None
        if abs(float(np.median(fs[l1_start:l1_end])) - LEADER_HZ - offset) > _LEADER_TOL_HZ / 2:
            return None
        # Refine the edge with the offset-corrected midpoint, then read raw medians.
        mid = (LEADER_HZ + SYNC_HZ) / 2 + offset
        k = j
        while k > j - 8 and fs[k - 1] < mid:
            k -= 1
        while k < j + 8 and fs[k] >= mid:
            k += 1
        p = base + k

        def tone(a_ms: float, b_ms: float) -> float:
            return float(np.median(self._seg(p + ms(a_ms), p + ms(b_ms)))) - offset

        if abs(tone(-LEADER_MS - BREAK_MS + 2, -LEADER_MS - 2) - SYNC_HZ) > _BREAK_TOL_HZ:
            return None
        bits = [tone(i * VIS_BIT_MS + 5, (i + 1) * VIS_BIT_MS - 5) for i in range(VIS_BITS)]
        if abs(bits[0] - SYNC_HZ) > _BIT_TOL_HZ or abs(bits[-1] - SYNC_HZ) > _BIT_TOL_HZ:
            return None
        # Data bits by side of 1200 Hz: in weak signals the discriminator pulls
        # 1100/1300 towards the band centre, so fixed windows would miss them.
        data = []
        for hz in bits[1:-1]:
            if abs(hz - SYNC_HZ) < _BIT_MIN_SPLIT_HZ or abs(hz - SYNC_HZ) > _BIT_MAX_SPLIT_HZ:
                return None
            data.append(1 if hz < SYNC_HZ else 0)
        if sum(data) % 2:
            return None
        mode = BY_VIS.get(sum(bit << i for i, bit in enumerate(data[:7])))
        if mode is None:
            return None
        return mode, p, offset

    # -- picture ------------------------------------------------------------

    def _start_picture(self, mode: Mode, p: int, offset: float) -> None:
        spm = self._spm
        self._mode = mode
        self._offset = offset
        self._line = 0
        self._line_len = mode.line_ms * spm
        self._sync_off = sync_offset_ms(mode) * spm
        self._sync_len = max(2, round(mode.sync_ms * spm))
        self._guard = max(2, self._sync_len // 2)
        # Absolute (fractional) start of line 0.
        self._t0 = p + VIS_BITS * VIS_BIT_MS * spm + mode.start_ms * spm
        self._good: list[tuple[int, float]] = []
        self._outliers: list[tuple[int, float]] = []
        self._missing = 0
        self._miss_limit = max(_LOSS_MIN_LINES, math.ceil(_LOSS_MS / mode.line_ms))
        self._emitted = 0
        self._unconfirmed: list[Row] = []
        # Lines rendered into _unconfirmed: (line, Robot 36 held state before it),
        # so a relock can render them again at the corrected timing.
        self._pending_lines: list[tuple[int, object]] = []
        self._held: tuple[np.ndarray, np.ndarray] | None = None  # Robot 36 even line
        self._keep_from = self._t0 - self._ms(_WIDE_SYNC_WINDOW_MS) - 2 * self._guard
        self._layouts = []
        for parity in (0, 1):
            layout, off = [], 0.0
            for seg in line_segments(mode, parity):
                if seg.channel:
                    layout.append((seg.channel, off * spm, seg.ms * spm))
                off += seg.ms
            self._layouts.append(layout)

    def _fit(self, k: int) -> tuple[float, float] | None:
        """Regression over the recent good syncs -> (position of line k, line length)."""
        pts = self._good[-_FIT_POINTS:]
        if len(pts) < _FIT_MIN_POINTS:
            return None
        ks = np.array([p[0] for p in pts], dtype=np.float64)
        xs = np.array([p[1] for p in pts])
        km = ks.mean()
        slope = float(np.sum((ks - km) * (xs - xs.mean())) / np.sum((ks - km) ** 2))
        return float(xs.mean() + slope * (k - km)), slope

    def _predict(self, k: int) -> float:
        fit = self._fit(k)
        if fit is not None:
            return fit[0]
        if self._good:
            k0, x0 = self._good[-1]
            return x0 + (k - k0) * self._line_len
        return self._t0 + self._sync_off + k * self._line_len

    def _find_sync(self, pred: float, window: int) -> tuple[float, float]:
        """Matched filter for the sync pulse around ``pred`` -> (position, quality)."""
        s, g = self._sync_len, self._guard
        lo = round(pred) - window
        f = self._seg(lo - g, round(pred) + window + s + g + 1)
        low = (f - self._offset < _SYNC_THRESHOLD_HZ).astype(np.float64)
        c = np.concatenate([[0.0], np.cumsum(low)])
        x = np.arange(g, len(low) - s - g + 1)
        score = 2 * c[x + s] - 2 * c[x] - (c[x + s + g] - c[x - g])
        best = score.max()
        at = x[score == best].mean()
        return lo - g + float(at), float(best) / s

    def _decode_line(self, events: list[Event]) -> bool:
        mode, k = self._mode, self._line
        pred = self._predict(k)
        # Wide window until locked and while syncs are missing (so a timing
        # jump, e.g. a dropped audio buffer, can be re-acquired), tight otherwise.
        window = (
            self._ms(_WIDE_SYNC_WINDOW_MS)
            if not self._good or self._missing or self._outliers
            else max(self._ms(2), round(0.005 * self._line_len))
        )
        line_end = pred - self._sync_off + self._line_len
        need = max(line_end, pred + self._sync_len) + window + self._sync_len + 2 * self._guard + 2
        if self._end < need:
            return False
        if self._final_end is not None and line_end > self._final_end + 2:
            return False
        if self._barrier is not None:
            if line_end > self._barrier + 2:
                return False
        elif (
            self._final_end is None
            and need + self._ms(2 * LEADER_MS + BREAK_MS) > self._vis_cursor
        ):
            # Not until the VIS scan has passed this line: a new picture's leader
            # or break inside its sync window must end the picture, not be
            # mistaken for a sync (costs ~0.9 s of live latency).
            return False

        pos, quality = self._find_sync(pred, window)
        good = quality >= _SYNC_MIN_QUALITY
        relocked = False
        if good:
            tol = max(4.0, 0.002 * self._line_len)
            if self._fit(k) is not None and abs(pos - pred) > tol:
                good = relocked = self._relock(k, pos - pred, tol)
            else:
                self._outliers = []
            if good:
                self._good.append((k, pos))
        if relocked:
            # The held-back lines were placed with the old timing: redo them.
            self._render_pending()

        start, scale = self._placement(k, pred if not good else pos)
        self._pending_lines.append((k, self._held))
        self._unconfirmed.extend(self._rows(k, start, scale))
        if good:
            self._confirm(events)
            self._missing = 0
        else:
            self._missing += 1

        self._line += 1
        if self._missing >= self._miss_limit:
            self._finish(events, complete_ok=False)
            return True
        if self._line == mode.lines:
            if self._missing <= 2:
                self._confirm(events)
            self._finish(events, complete_ok=True)
            return True
        # Audio still needed: the next line's sync search, and every held-back
        # line (a relock renders them again).
        keep = start + self._line_len * scale - self._ms(_WIDE_SYNC_WINDOW_MS) - 2 * self._guard
        if self._pending_lines:
            first = self._pending_lines[0][0]
            keep = min(keep, self._placement(first, self._predict(first))[0])
        # A relock may move a held-back line by up to the wide sync window.
        self._keep_from = keep - self._ms(_WIDE_SYNC_WINDOW_MS) - 2 * self._guard
        return True

    def _placement(self, k: int, fallback: float) -> tuple[float, float]:
        """Line start and time scale of line ``k`` (regression if available)."""
        fit = self._fit(k)
        if fit is None:
            return fallback - self._sync_off, 1.0
        # Measured line length -> time scale of the sender (sample-rate offset).
        scale = min(max(fit[1] / self._line_len, 0.98), 1.02)
        return fit[0] - self._sync_off * scale, scale

    def _render_pending(self) -> None:
        if not self._pending_lines:
            return
        self._held = self._pending_lines[0][1]
        rows: list[Row] = []
        for k, _ in self._pending_lines:
            start, scale = self._placement(k, self._predict(k))
            rows.extend(self._rows(k, start, scale))
        self._unconfirmed = rows

    def _relock(self, k: int, residual: float, tol: float) -> bool:
        """A sync far from the fit: accept only after several agreeing lines."""
        if self._outliers and (
            self._outliers[-1][0] != k - 1 or abs(self._outliers[-1][1] - residual) > tol
        ):
            self._outliers = []
        self._outliers.append((k, residual))
        if len(self._outliers) < _RELOCK_LINES:
            return False
        # The sender jumped: keep the slant estimate, shift the history by the
        # agreed offset and accept the agreeing outliers.
        shift = float(np.mean([r for _, r in self._outliers]))
        self._good = [(kk, x + shift) for kk, x in self._good[-_FIT_POINTS:]]
        self._good += [(kk, self._predict(kk) + r - shift) for kk, r in self._outliers[:-1]]
        self._outliers = []
        return True

    def _confirm(self, events: list[Event]) -> None:
        events.extend(self._unconfirmed)
        self._emitted += len(self._unconfirmed)
        self._unconfirmed = []
        self._pending_lines = []

    def _finish(self, events: list[Event], complete_ok: bool) -> None:
        complete = complete_ok and self._emitted == self._mode.height
        events.append(End(complete=complete, rows=self._emitted))
        self._mode = None

    def _levels(self, start: float, offset: float, length: float) -> np.ndarray:
        """Mean frequency of each pixel of a scan -> levels 0..255."""
        w = self._mode.width
        edges = start + offset + np.arange(w + 1) * (length / w)
        i0 = math.floor(edges[0]) - 1
        i1 = math.ceil(edges[-1]) + 2
        f = self._seg(i0, i1)
        c = np.concatenate([[0.0], np.cumsum(f)])
        # f[j] covers [i0 + j - 0.5, i0 + j + 0.5).
        ci = np.interp(edges - (i0 - 0.5), np.arange(len(c)), c)
        hz = np.diff(ci) / np.diff(edges) - self._offset
        return np.clip((hz - BLACK_HZ) * _LEVEL_PER_HZ, 0.0, 255.0)

    def _rows(self, k: int, start: float, scale: float) -> list[Row]:
        mode = self._mode
        ch = {
            name: self._levels(start, off * scale, length * scale)
            for name, off, length in self._layouts[k % 2]
        }
        rpl = mode.rows_per_line
        if mode.color in ("gbr", "rgb_scottie"):
            pixels = [_rgb(ch["r"], ch["g"], ch["b"])]
        elif mode.color == "yuv_robot72":
            pixels = [_yuv_to_rgb(ch["y"], ch["u"], ch["v"])]
        elif mode.color == "yuv_pd":
            pixels = [
                _yuv_to_rgb(ch["y0"], ch["u"], ch["v"]),
                _yuv_to_rgb(ch["y1"], ch["u"], ch["v"]),
            ]
        else:  # yuv_robot36: even line Y + V, odd line Y + U; the pair shares chroma
            if k % 2 == 0:
                self._held = (ch["y"], ch["v"])
                return []
            y0, v = self._held
            self._held = None
            return [
                Row(k - 1, _yuv_to_rgb(y0, ch["u"], v).tobytes()),
                Row(k, _yuv_to_rgb(ch["y"], ch["u"], v).tobytes()),
            ]
        return [Row(k * rpl + i, px.tobytes()) for i, px in enumerate(pixels)]
