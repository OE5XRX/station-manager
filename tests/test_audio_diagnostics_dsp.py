import math
import struct

from station_agent.audio import diagnostics as d


def _sine_s16(freq, peak_amp, dur_ms, rate):
    n = int(rate * dur_ms / 1000)
    samples = [
        max(-32768, min(32767, int(peak_amp * math.sin(2 * math.pi * freq * i / rate))))
        for i in range(n)
    ]
    return struct.pack(f"<{n}h", *samples)


def test_full_scale_sine_is_0_dbfs_peak():
    pcm = _sine_s16(1000, 32767, 300, 8000)
    rms, peak, silent = d.rms_peak_dbfs(pcm)
    assert silent is False
    assert peak == -0.0 or abs(peak) < 0.1  # full-scale peak ~ 0 dBFS
    assert abs(rms - (-3.01)) < 0.3  # sine RMS is ~ -3 dBFS below peak


def test_minus20_sine_peaks_at_minus20():
    pcm = _sine_s16(1000, 32767 * 10 ** (-20 / 20), 300, 8000)
    rms, peak, silent = d.rms_peak_dbfs(pcm)
    assert abs(peak - (-20.0)) < 0.3


def test_silence_reports_none_not_zero():
    rms, peak, silent = d.rms_peak_dbfs(b"\x00\x00" * 1000)
    assert silent is True and rms is None and peak is None


def test_empty_pcm_is_silent():
    assert d.rms_peak_dbfs(b"") == (None, None, True)


def test_generate_sine_round_trips_to_requested_level():
    pcm = d.generate_sine_pcm(1000, -20.0, 300, 16000)
    rms, peak, silent = d.rms_peak_dbfs(pcm)
    assert silent is False
    assert abs(peak - (-20.0)) < 0.5
    assert len(pcm) == int(16000 * 300 / 1000) * 2
