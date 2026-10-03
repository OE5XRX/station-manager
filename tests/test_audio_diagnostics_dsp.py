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


def test_diag_stream_ref_is_reserved_high_value():
    assert d.DIAG_STREAM_REF == 0xFFFE  # cannot collide with small real slot/mic refs


def test_build_cd_taps_projects_d_from_sink_db():
    gains = {"sink_volume_linear": 0.40, "sink_volume_db": -7.96}
    taps = d.build_cd_taps(-20.0, -20.0, False, gains, 16000, 300)
    assert [t["point"] for t in taps] == ["C", "D"]
    assert taps[0]["computed"] is False and taps[0]["silent"] is False
    assert taps[1]["computed"] is True
    assert taps[1]["rms_dbfs"] == -27.96 and taps[1]["peak_dbfs"] == -27.96


def test_build_cd_taps_silent_c_makes_d_silent():
    gains = {"sink_volume_linear": 0.40, "sink_volume_db": -7.96}
    taps = d.build_cd_taps(None, None, True, gains, 16000, 300)
    assert taps[1]["silent"] is True and taps[1]["rms_dbfs"] is None


def test_build_cd_taps_muted_sink_makes_d_silent():
    gains = {"sink_volume_linear": 0.0, "sink_volume_db": None}
    taps = d.build_cd_taps(-20.0, -20.0, False, gains, 16000, 300)
    assert taps[1]["silent"] is True and taps[1].get("note") == "sink muted"


def test_build_cd_taps_unreadable_sink_makes_d_unavailable():
    gains = {"sink_volume_linear": None, "sink_volume_db": None}
    taps = d.build_cd_taps(-20.0, -20.0, False, gains, 16000, 300)
    assert taps[1]["silent"] is False and taps[1]["rms_dbfs"] is None
    assert "unavailable" in taps[1]["note"]
