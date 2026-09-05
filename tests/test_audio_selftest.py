# tests/test_audio_selftest.py
"""selftest audio orchestration logic (pipelines built + Goertzel verdicts), no GStreamer."""

import math
import struct

from station_agent.audio import selftest


def _pcm_s16(freq, rate, n, amp=15000):
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)
    )


def test_build_rx_check_argv_roundtrips_opus_to_pcm():
    argv = selftest.build_rx_check_argv("oe5xrx.slot1", 8000)
    j = " ".join(argv)
    assert "pipewiresrc" in j and "target-object=oe5xrx.slot1" in j
    assert "opusenc" in j and "opusdec" in j  # proves the full RX Opus roundtrip
    assert "format=S16LE" in j and "rate=8000" in j
    assert "fdsink" in j


def test_build_tx_play_argv_injects_distinct_tone():
    argv = selftest.build_tx_play_argv("oe5xrx.slot1.tx", 1500, 8000)
    j = " ".join(argv)
    assert "audiotestsrc" in j and "freq=1500" in j
    assert "opusenc" in j and "opusdec" not in j
    assert "pipewiresink" in j and "target-object=oe5xrx.slot1.tx" in j


def test_analyze_pcm_finds_dominant_frequency():
    rate = 8000
    pcm = _pcm_s16(1000, rate, 1600)
    assert selftest.analyze_pcm(pcm, [500, 1000, 1500, 2000], rate) == 1000


class FakeBackend:
    def __init__(self, nodes):
        self._nodes = nodes

    def resolve_node(self, slot, direction):
        return self._nodes.get((slot, direction))


def test_run_audio_passes_when_both_tones_detected():
    rate = 8000
    backend = FakeBackend({(1, "rx"): "oe5xrx.slot1", (1, "tx"): "oe5xrx.slot1.tx"})
    calls = {"played": []}

    def capture(argv, duration):
        # RX check taps the 1 kHz shim tone
        return _pcm_s16(1000, rate, 1600)

    def play(argv, duration):
        calls["played"].append(argv)

    def capture_after_play(argv, duration):
        # after playing 1500 Hz into TX, the reverse cable exposes it on the capture
        return _pcm_s16(1500, rate, 1600)

    rc = selftest.run_audio(
        slot=1,
        tx_freq=1500,
        rate=rate,
        backend=backend,
        capture=capture,
        play=play,
        capture_tx=capture_after_play,
        duration=0.2,
    )
    assert rc == 0
    assert calls["played"], "TX tone was never played"


def test_run_audio_fails_when_rx_tone_absent():
    rate = 8000
    backend = FakeBackend({(1, "rx"): "n", (1, "tx"): "n.tx"})
    rc = selftest.run_audio(
        slot=1,
        tx_freq=1500,
        rate=rate,
        backend=backend,
        capture=lambda a, d: _pcm_s16(3000, rate, 1600),  # wrong tone → RX fails
        play=lambda a, d: None,
        capture_tx=lambda a, d: _pcm_s16(1500, rate, 1600),
        duration=0.2,
    )
    assert rc == 1


def test_run_audio_fails_when_node_unresolved():
    backend = FakeBackend({})  # no nodes
    rc = selftest.run_audio(
        slot=1,
        tx_freq=1500,
        rate=8000,
        backend=backend,
        capture=lambda a, d: b"",
        play=lambda a, d: None,
        capture_tx=lambda a, d: b"",
        duration=0.2,
    )
    assert rc == 1
