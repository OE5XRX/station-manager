# tests/test_audio_goertzel.py
"""Pure-Python Goertzel single-frequency power detector for the audio selftest."""
import math

from station_agent.audio.goertzel import goertzel_power, dominant_bin


def _sine(freq, rate, n, amp=0.5):
    return [amp * math.sin(2 * math.pi * freq * i / rate) for i in range(n)]


def test_power_peaks_at_the_tone_frequency():
    rate, n = 8000, 1600
    tone = _sine(1000, rate, n)
    on = goertzel_power(tone, 1000, rate)
    off = goertzel_power(tone, 1500, rate)
    assert on > off * 20  # the 1 kHz bin dominates a nearby empty bin


def test_dominant_bin_selects_the_present_tone():
    rate, n = 8000, 1600
    tone = _sine(1500, rate, n)
    best = dominant_bin(tone, [1000, 1500, 2000], rate)
    assert best == 1500


def test_silence_has_negligible_power():
    rate, n = 8000, 1600
    silence = [0.0] * n
    assert goertzel_power(silence, 1000, rate) < 1e-6


def test_accepts_int_pcm_samples():
    rate, n = 8000, 1600
    tone = [int(16000 * math.sin(2 * math.pi * 1000 * i / rate)) for i in range(n)]
    on = goertzel_power(tone, 1000, rate)
    off = goertzel_power(tone, 500, rate)
    assert on > off * 20
