"""SSTV streaming decoder (station_agent/sstv/decoder.py).

All signals are synthetic: ``encode()`` of a deterministic test picture, with
white noise, resampling and truncation applied before decoding.
"""

import numpy as np
import pytest

from station_agent.sstv import decoder as decoder_module
from station_agent.sstv.decoder import End, Row, Start, StreamDecoder
from station_agent.sstv.encoder import encode
from station_agent.sstv.modes import MODES, sync_offset_ms

SR = 8000
CHUNK = SR // 50  # 20 ms


def _picture(w, h):
    """Colour bars on top, RGB gradients below."""
    x = np.arange(w)[None, :].repeat(h, 0)
    y = np.arange(h)[:, None].repeat(w, 1)
    bars = np.array(
        [
            (191, 191, 191),
            (191, 191, 0),
            (0, 191, 191),
            (0, 191, 0),
            (191, 0, 191),
            (191, 0, 0),
            (0, 0, 191),
            (16, 16, 16),
        ]
    )
    img = np.zeros((h, w, 3), dtype=np.int64)
    img[:] = bars[(x * 8) // w]
    lower = y >= h // 2
    img[lower, 0] = (x * 255 // (w - 1))[lower]
    img[lower, 1] = ((y - h // 2) * 255 // (h - h // 2 - 1))[lower]
    img[lower, 2] = 255 - (x * 255 // (w - 1))[lower]
    return img.astype(np.uint8)


_PCM_CACHE = {}


def _clean(key):
    if key not in _PCM_CACHE:
        m = MODES[key]
        pic = _picture(m.width, m.height)
        pcm = np.frombuffer(encode(m, pic.tobytes()), dtype="<i2").astype(np.float64)
        _PCM_CACHE[key] = (pic, pcm)
    return _PCM_CACHE[key]


def _awgn(signal, snr_db, rng):
    power = np.mean(signal**2)
    return signal + rng.normal(0.0, np.sqrt(power / 10 ** (snr_db / 10)), len(signal))


def _noise(seconds, rng, std=0.3 * 32767):
    return rng.normal(0.0, std, int(seconds * SR))


def _to_i16(x):
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


def _run(audio, chunk=CHUNK):
    dec = StreamDecoder(SR)
    audio = _to_i16(audio)
    events = []
    for i in range(0, len(audio), chunk):
        events += dec.feed(audio[i : i + chunk])
    events += dec.flush()
    return events


def _rows(events, mode):
    rows = [e for e in events if isinstance(e, Row)]
    return rows, np.frombuffer(b"".join(r.rgb for r in rows), dtype=np.uint8).reshape(
        len(rows), mode.width, 3
    )


def _assert_complete(events, key, max_err):
    m = MODES[key]
    pic, _ = _clean(key)
    starts = [e for e in events if isinstance(e, Start)]
    ends = [e for e in events if isinstance(e, End)]
    assert [s.mode for s in starts] == [m]
    assert ends == [End(complete=True, rows=m.height)]
    rows, got = _rows(events, m)
    assert [r.index for r in rows] == list(range(m.height))
    assert all(len(r.rgb) == m.width * 3 for r in rows)
    # Event order: Start, then rows, then End.
    assert isinstance(events[0], Start) and isinstance(events[-1], End)
    err = np.mean(np.abs(got.astype(np.int64) - pic.astype(np.int64)))
    assert err < max_err, f"{key}: mean abs pixel error {err:.1f}"


@pytest.mark.parametrize("key", sorted(MODES))
def test_roundtrip_every_mode(key):
    rng = np.random.default_rng(1)
    _, pcm = _clean(key)
    audio = np.concatenate([_noise(0.5, rng, 300), pcm, _noise(1.0, rng, 300)])
    _assert_complete(_run(_awgn(audio, 20, rng)), key, 12)


def _resample(x, factor):
    """Stretch ``x`` to ``len(x) * factor`` samples (a slow/fast sound card)."""
    n = int(len(x) * factor)
    return np.interp(np.arange(n) / factor, np.arange(len(x)), x)


@pytest.mark.parametrize("key", sorted(MODES))
def test_sample_rate_offset_tolerated(key):
    rng = np.random.default_rng(2)
    _, pcm = _clean(key)
    audio = np.concatenate([_noise(0.5, rng, 300), _resample(pcm, 1.001), _noise(1.0, rng, 300)])
    _assert_complete(_run(_awgn(audio, 20, rng)), key, 20)


def test_sample_rate_offset_negative_tolerated():
    rng = np.random.default_rng(3)
    _, pcm = _clean("scottiedx")
    audio = np.concatenate([_noise(0.5, rng, 300), _resample(pcm, 0.999), _noise(1.0, rng, 300)])
    _assert_complete(_run(_awgn(audio, 20, rng)), "scottiedx", 20)


def test_signal_loss_midpicture():
    rng = np.random.default_rng(4)
    m = MODES["martin1"]
    _, pcm = _clean("martin1")
    vis = int(0.91 * SR)
    cut = vis + int(0.4 * (len(pcm) - vis))
    audio = np.concatenate([_noise(0.5, rng, 300), pcm[:cut], _noise(10.0, rng)])
    events = _run(_awgn(audio, 20, rng))
    starts = [e for e in events if isinstance(e, Start)]
    ends = [e for e in events if isinstance(e, End)]
    assert len(starts) == 1 and len(ends) == 1
    assert ends[0].complete is False
    assert abs(ends[0].rows - 0.4 * m.height) <= 5
    rows, _ = _rows(events, m)
    assert len(rows) == ends[0].rows
    assert isinstance(events[-1], End)


def test_flush_mid_picture_ends_incomplete():
    m = MODES["robot36"]
    _, pcm = _clean("robot36")
    half = int(0.91 * SR) + (len(pcm) - int(0.91 * SR)) // 2
    dec = StreamDecoder(SR)
    events = dec.feed(_to_i16(pcm[:half]))
    events += dec.flush()
    end = events[-1]
    assert isinstance(end, End) and end.complete is False
    assert abs(end.rows - m.height // 2) <= 4
    assert dec.flush() == []


def test_two_pictures_back_to_back():
    rng = np.random.default_rng(5)
    _, a = _clean("robot36")
    _, b = _clean("martin2")
    audio = np.concatenate([_noise(0.5, rng, 300), a, b, _noise(1.0, rng, 300)])
    events = _run(_awgn(audio, 20, rng))
    kinds = [type(e).__name__ for e in events if not isinstance(e, Row)]
    assert kinds == ["Start", "End", "Start", "End"]
    starts = [e.mode.key for e in events if isinstance(e, Start)]
    assert starts == ["robot36", "martin2"]
    assert [e for e in events if isinstance(e, End)] == [
        End(complete=True, rows=240),
        End(complete=True, rows=256),
    ]


def _speech_like(seconds, rng):
    """Voice stand-in: three formant tones hopping every 50-250 ms, syllabic envelope."""
    n = int(seconds * SR)
    out = np.zeros(n)
    pos = 0
    phases = np.zeros(3)
    while pos < n:
        seg = min(n - pos, int(rng.uniform(0.05, 0.25) * SR))
        freqs = (rng.uniform(250, 900), rng.uniform(900, 2500), rng.uniform(2000, 3500))
        amps = (1.0, rng.uniform(0.3, 0.8), rng.uniform(0.1, 0.4))
        t = np.arange(seg)
        for i, (f, a) in enumerate(zip(freqs, amps, strict=True)):
            out[pos : pos + seg] += a * np.sin(phases[i] + 2 * np.pi * f * t / SR)
            phases[i] += 2 * np.pi * f * seg / SR
        pos += seg
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4.0 * np.arange(n) / SR)
    return 9000 * out * env + rng.normal(0, 300, n)


def test_noise_and_voice_never_start():
    rng = np.random.default_rng(6)
    dec = StreamDecoder(SR)
    events = []
    # 30 min of white noise, fed in 20 ms chunks, generated 60 s at a time.
    for _ in range(30):
        block = _to_i16(_noise(60.0, rng))
        for i in range(0, len(block), CHUNK):
            events += dec.feed(block[i : i + CHUNK])
    voice = _to_i16(_speech_like(60.0, rng))
    for i in range(0, len(voice), CHUNK):
        events += dec.feed(voice[i : i + CHUNK])
    events += dec.flush()
    assert events == []


def _tone(hz, ms):
    t = np.arange(int(ms * SR / 1000))
    return 0.9 * 32767 * np.sin(2 * np.pi * hz * t / SR)


def test_bad_vis_parity_rejected():
    rng = np.random.default_rng(7)
    _, pcm = _clean("martin1")
    bad = pcm.copy()
    # Parity bit = 9th VIS bit (start bit at 610 ms): 850..880 ms. Martin M1
    # (VIS 44 = 0101100b) has odd weight, so parity is 1 (1100 Hz); send 0.
    a, b = int(0.850 * SR), int(0.880 * SR)
    bad[a:b] = _tone(1300, 30)[: b - a]
    audio = np.concatenate([_noise(0.5, rng, 300), bad, _noise(1.0, rng, 300)])
    events = _run(_awgn(audio, 30, rng))
    assert not [e for e in events if isinstance(e, Start)]
    # ...while the unmodified signal is accepted (the test is not vacuous).
    good = np.concatenate([_noise(0.5, rng, 300), pcm, _noise(1.0, rng, 300)])
    assert [e for e in _run(_awgn(good, 30, rng)) if isinstance(e, Start)]


def test_missing_first_leader_half_rejected():
    rng = np.random.default_rng(8)
    _, pcm = _clean("robot36")
    cut = pcm.copy()
    cut[: int(0.3 * SR)] = _noise(0.3, rng)
    events = _run(np.concatenate([_noise(0.5, rng), cut]))
    assert not [e for e in events if isinstance(e, Start)]


@pytest.mark.parametrize("chunk", [7, 4099])
def test_chunking_does_not_matter(chunk):
    m = MODES["robot36"]
    _, pcm = _clean("robot36")
    events = _run(pcm, chunk=chunk)
    assert events[-1] == End(complete=True, rows=240)
    _, got = _rows(events, m)
    _, ref = _rows(_run(pcm), m)
    assert got.shape == ref.shape == (240, 320, 3)
    # Same pixels as the 20 ms reference (float rounding may flip a level).
    assert np.max(np.abs(got.astype(int) - ref.astype(int))) <= 1


def _shift(x, hz):
    """Shift every frequency in ``x`` by ``hz`` (mistuned SSB receiver)."""
    n = len(x)
    h = np.zeros(n)
    h[0] = 1
    h[1 : (n + 1) // 2] = 2
    if n % 2 == 0:
        h[n // 2] = 1
    analytic = np.fft.ifft(np.fft.fft(x) * h)
    return np.real(analytic * np.exp(2j * np.pi * hz * np.arange(n) / SR))


@pytest.mark.parametrize("hz", [-60, 60])
def test_receiver_frequency_offset_tolerated(hz):
    rng = np.random.default_rng(9)
    _, pcm = _clean("martin1")
    audio = np.concatenate([_noise(0.5, rng, 300), _shift(pcm, hz), _noise(1.0, rng, 300)])
    _assert_complete(_run(_awgn(audio, 20, rng)), "martin1", 12)


@pytest.mark.parametrize("gap_s", [0.0, 1.0, 2.0])
def test_new_picture_right_after_a_lost_one(gap_s):
    # Signal dies mid-picture and a new picture starts before the old one is
    # declared lost (~3 s): the new one must still be found.
    rng = np.random.default_rng(10)
    _, a = _clean("martin1")
    _, b = _clean("robot36")
    vis = int(0.91 * SR)
    cut = vis + int(0.3 * (len(a) - vis))
    audio = np.concatenate(
        [_noise(0.5, rng, 300), a[:cut], _noise(gap_s, rng, 300), b, _noise(1.0, rng, 300)]
    )
    events = _run(_awgn(audio, 20, rng))
    summary = [
        (type(e).__name__, e.mode.key if isinstance(e, Start) else None)
        for e in events
        if not isinstance(e, Row)
    ]
    assert summary == [("Start", "martin1"), ("End", None), ("Start", "robot36"), ("End", None)]
    ends = [e for e in events if isinstance(e, End)]
    assert ends[0].complete is False and abs(ends[0].rows - 0.3 * 256) <= 5
    assert ends[1] == End(complete=True, rows=240)


@pytest.mark.parametrize("key", ["martin1", "scottie1"])
@pytest.mark.parametrize("drop", [32, 64])  # 4 ms, 8 ms
def test_relock_after_a_timing_jump_keeps_decoding(key, drop):
    # A sound-card glitch drops audio mid-picture: syncs jump, the decoder must
    # relock (not lose the picture) and keep the rest of the picture clean.
    rng = np.random.default_rng(11)
    m = MODES[key]
    pic, pcm = _clean(key)
    vis = int(0.91 * SR)
    line = m.line_ms * SR / 1000
    jump = vis + int(100.5 * line)
    sig = np.concatenate([pcm[:jump], pcm[jump + drop :]])
    audio = np.concatenate([_noise(0.5, rng, 300), _resample(sig, 1.001), _noise(1.0, rng, 300)])
    events = _run(_awgn(audio, 20, rng))
    assert events[-1] == End(complete=True, rows=256)
    _, got = _rows(events, m)
    err = np.abs(got.astype(int) - pic.astype(int)).mean(axis=(1, 2))
    # Row 100 holds the jump itself (its audio is broken). The rows after it
    # were decoded while the relock was still unconfirmed; they are rendered
    # again at the corrected timing, so they must be clean too.
    assert err[95:100].max() < 12
    assert err[101:110].max() < 12
    assert err[110:].mean() < 12


@pytest.mark.parametrize("shift", [-4, 0, 4])
def test_same_mode_picture_aligned_with_a_lost_one(shift):
    # The second picture's syncs land exactly where the lost one's would be:
    # its VIS must still end the old picture instead of being glued into it.
    rng = np.random.default_rng(12)
    m = MODES["martin1"]
    _, pcm = _clean("martin1")
    line = m.line_ms * SR / 1000
    # The first picture is cut so that the second one's line 0 starts exactly
    # where the first one's line 76 would (VIS blocks have the same length).
    cut = round(76 * line) + shift
    audio = np.concatenate([_noise(0.5, rng, 300), pcm[:cut], pcm, _noise(1.0, rng, 300)])
    events = _run(_awgn(audio, 20, rng))
    kinds = [(type(e).__name__) for e in events if not isinstance(e, Row)]
    assert kinds == ["Start", "End", "Start", "End"]
    ends = [e for e in events if isinstance(e, End)]
    assert ends[0].complete is False and ends[0].rows <= 76 - 2
    assert ends[1] == End(complete=True, rows=256)


@pytest.mark.parametrize("key", ["scottie1", "robot36"])
@pytest.mark.parametrize("shift", [-6, 0, 6])
def test_new_vis_break_on_the_old_sync_grid_is_not_a_sync(key, shift):
    # The new picture's 10 ms break (1200 Hz) lands where the lost picture's
    # line-60 sync (9 ms) is expected: it must not confirm leader-gray rows.
    rng = np.random.default_rng(13)
    m = MODES[key]
    pic, pcm = _clean(key)
    vis = 0.91 * SR
    line = m.line_ms * SR / 1000
    sync_at = vis + m.start_ms * SR / 1000 + 60 * line + sync_offset_ms(m) * SR / 1000
    cut = round(sync_at - 0.300 * SR) + shift  # new leader 1 (300 ms), then the break
    audio = np.concatenate([_noise(0.5, rng, 300), pcm[:cut], pcm, _noise(1.0, rng, 300)])
    events = _run(_awgn(audio, 20, rng))
    kinds = [(type(e).__name__) for e in events if not isinstance(e, Row)]
    assert kinds == ["Start", "End", "Start", "End"]
    first = []
    for e in events:
        if isinstance(e, Row):
            first.append(e)
        if isinstance(e, End):
            break
    assert first, "lost picture yielded no rows"
    got = np.frombuffer(b"".join(r.rgb for r in first), np.uint8).reshape(len(first), m.width, 3)
    idx = [r.index for r in first]
    err = np.abs(got.astype(int) - pic[idx].astype(int)).mean(axis=(1, 2))
    assert err.max() < 25, f"rows {np.array(idx)[err >= 25]} are not picture content"
    assert [e for e in events if isinstance(e, End)][1] == End(complete=True, rows=m.height)


@pytest.fixture(autouse=True)
def _strict_decoder(monkeypatch):
    # In tests an internal invariant failure must fail loudly, not be survived.
    monkeypatch.setattr(decoder_module, "STRICT", True)


def test_internal_error_ends_picture_and_decoding_continues(monkeypatch, caplog):
    monkeypatch.setattr(decoder_module, "STRICT", False)
    _, a = _clean("robot36")
    _, b = _clean("martin2")
    dec = StreamDecoder(SR)
    real = StreamDecoder._levels
    calls = {"n": 0}

    def flaky(self, *args):
        calls["n"] += 1
        if calls["n"] == 50:  # once, mid-picture
            raise decoder_module._BufferError("boom")
        return real(self, *args)

    monkeypatch.setattr(StreamDecoder, "_levels", flaky)
    audio = _to_i16(np.concatenate([a, b, np.zeros(SR)]))
    events = []
    for i in range(0, len(audio), CHUNK):
        events += dec.feed(audio[i : i + CHUNK])
    events += dec.flush()
    kinds = [type(e).__name__ for e in events if not isinstance(e, Row)]
    assert kinds == ["Start", "End", "Start", "End"]
    ends = [e for e in events if isinstance(e, End)]
    assert ends[0].complete is False
    assert ends[1] == End(complete=True, rows=256)
    assert "invariant failed" in caplog.text
