import math
import struct

from station_agent.audio import tx_meter


def _f32(samples):
    return struct.pack(f"<{len(samples)}f", *samples)


def test_chunk_bytes():
    assert tx_meter.chunk_bytes(16000) == 2000 * 4


def test_silence_is_none_and_no_limiting():
    r = tx_meter.compute_meter(_f32([0.0] * 100), limiter_threshold=0.25, limiter_ratio=1000.0)
    assert r == {"peak_dbfs": None, "rms_dbfs": None, "gain_reduction_db": 0.0, "limiting": False}


def test_below_ceiling_passes_untouched():
    r = tx_meter.compute_meter(
        _f32([0.1, -0.1] * 50), limiter_threshold=0.25, limiter_ratio=1000.0
    )
    assert r["peak_dbfs"] == -20.0
    assert r["gain_reduction_db"] == 0.0 and r["limiting"] is False


def test_over_ceiling_is_held_at_ceiling_with_gain_reduction():
    r = tx_meter.compute_meter(
        _f32([1.0, -1.0] * 50), limiter_threshold=0.25, limiter_ratio=1000.0
    )
    assert r["peak_dbfs"] == round(20 * math.log10(0.25 + 0.75 / 1000), 1)  # ~ -12.0
    assert r["gain_reduction_db"] == round(20 * math.log10(1.0 / (0.25 + 0.75 / 1000)), 1)
    assert r["limiting"] is True


def test_no_limiter_models_s16_clip_at_full_scale():
    r = tx_meter.compute_meter(_f32([2.0] * 10), limiter_threshold=None, limiter_ratio=1.0)
    assert r["peak_dbfs"] == 0.0
    assert r["limiting"] is False  # no limiter in path; clip is reported via peak 0 dBFS


def test_nan_inf_samples_ignored():
    r = tx_meter.compute_meter(
        _f32([float("nan"), float("inf"), 0.1]), limiter_threshold=0.25, limiter_ratio=1000.0
    )
    assert r["peak_dbfs"] == -20.0


def test_partial_trailing_bytes_ignored():
    r = tx_meter.compute_meter(
        _f32([0.1]) + b"\x00\x01", limiter_threshold=0.25, limiter_ratio=1000.0
    )
    assert r["peak_dbfs"] == -20.0
