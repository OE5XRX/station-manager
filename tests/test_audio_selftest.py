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
    # valid opusenc audio-type nick (voip is invalid → gst rejects the pipeline; Session E)
    assert "audio-type=voice" in j and "audio-type=voip" not in j
    assert "format=S16LE" in j and "rate=8000" in j
    assert "fdsink" in j


def test_build_tx_play_argv_is_a_live_opus_roundtrip_into_the_sink():
    argv = selftest.build_tx_play_argv("oe5xrx.slot1.tx", 1500, 8000)
    j = " ".join(argv)
    assert "audiotestsrc" in j and "freq=1500" in j
    assert "is-live=true" in j  # keeps the tone flowing while the reverse tap records
    # Full Opus roundtrip (encode AND decode) — raw PCM is what pipewiresink consumes;
    # feeding it opusenc output directly is a caps mismatch that never links.
    assert "opusenc" in j and "opusdec" in j
    assert j.index("opusenc") < j.index("opusdec")
    assert "audio-type=voice" in j and "audio-type=voip" not in j
    assert "pipewiresink" in j and "target-object=oe5xrx.slot1.tx" in j


def test_build_tx_capture_argv_taps_the_raw_reverse_cable():
    # The injected TX tone appears on the aloop dev0 capture (cable B), NOT the RX PipeWire
    # node (cable A / the 1 kHz shim) — so the TX tap is a raw ALSA hw capture.
    argv = selftest.build_tx_capture_argv("hw:7,0,0", 8000, 1.0)
    j = " ".join(argv)
    assert argv[0] == "arecord"
    assert "-D hw:7,0,0" in j
    assert "S16_LE" in j and "8000" in j
    assert "pipewiresrc" not in j and "oe5xrx.slot1" not in j  # not the RX node


def test_analyze_pcm_finds_dominant_frequency():
    rate = 8000
    pcm = _pcm_s16(1000, rate, 1600)
    assert selftest.analyze_pcm(pcm, [500, 1000, 1500, 2000], rate) == 1000


def test_dominance_is_high_for_a_pure_tone_and_zero_for_silence():
    rate = 8000
    pcm = _pcm_s16(1500, rate, 1600)
    assert selftest.dominance(pcm, 1500, [1000, 500, 2000], rate) > selftest._LOUD_MARGIN
    assert selftest.dominance(b"", 1500, [1000], rate) == 0.0


class FakeProc:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class FakeBackend:
    def __init__(self, nodes, cards=None):
        self._nodes = nodes
        self._cards = cards or {}

    def resolve_node(self, slot, direction):
        return self._nodes.get((slot, direction))

    def alsa_card_for_slot(self, slot):
        return self._cards.get(slot)


def test_run_audio_passes_when_both_tones_detected():
    rate = 8000
    backend = FakeBackend({(1, "rx"): "oe5xrx.slot1", (1, "tx"): "oe5xrx.slot1.tx"}, cards={1: 7})
    calls = {"spawned": [], "tx_tap_argv": []}
    proc = FakeProc()

    def capture(argv, duration):
        # RX check taps the 1 kHz shim tone
        return _pcm_s16(1000, rate, 1600)

    def spawn(argv):
        calls["spawned"].append(argv)
        return proc

    def capture_tx(argv, duration):
        # The reverse-cable dev0 tap, recorded WHILE the 1500 Hz tone plays
        calls["tx_tap_argv"].append(argv)
        return _pcm_s16(1500, rate, 1600)

    rc = selftest.run_audio(
        slot=1,
        tx_freq=1500,
        rate=rate,
        backend=backend,
        capture=capture,
        capture_tx=capture_tx,
        spawn=spawn,
        wait_running=lambda: None,
        duration=0.2,
    )
    assert rc == 0
    assert calls["spawned"], "TX tone was never played (spawned)"
    assert proc.terminated, "TX tone process was not stopped after capture"
    # The reverse tap must target the raw aloop dev0 hw device derived from the card index.
    assert any("hw:7,0,0" in " ".join(a) for a in calls["tx_tap_argv"])


def test_run_audio_fails_when_rx_tone_absent():
    rate = 8000
    backend = FakeBackend({(1, "rx"): "n", (1, "tx"): "n.tx"}, cards={1: 7})
    rc = selftest.run_audio(
        slot=1,
        tx_freq=1500,
        rate=rate,
        backend=backend,
        capture=lambda a, d: _pcm_s16(3000, rate, 1600),  # wrong tone → RX fails
        capture_tx=lambda a, d: _pcm_s16(1500, rate, 1600),
        spawn=lambda a: FakeProc(),
        wait_running=lambda: None,
        duration=0.2,
    )
    assert rc == 1


def test_run_audio_fails_when_tx_tone_absent_on_reverse_tap():
    # TX injected but the reverse tap only shows the 1 kHz RX shim (the exact bug E found:
    # tapping cable A instead of the reverse cable B) → TX must FAIL, not pass on RX bleed.
    rate = 8000
    backend = FakeBackend({(1, "rx"): "oe5xrx.slot1", (1, "tx"): "oe5xrx.slot1.tx"}, cards={1: 7})
    rc = selftest.run_audio(
        slot=1,
        tx_freq=1500,
        rate=rate,
        backend=backend,
        capture=lambda a, d: _pcm_s16(1000, rate, 1600),  # RX ok
        capture_tx=lambda a, d: _pcm_s16(1000, rate, 1600),  # reverse tap shows 1 kHz, not 1500
        spawn=lambda a: FakeProc(),
        wait_running=lambda: None,
        duration=0.2,
    )
    assert rc == 1


def test_capture_salvages_pcm_on_timeout(monkeypatch):
    # The RX pipeline (pipewiresrc ! … ! fdsink) never self-terminates, so _capture
    # ALWAYS hits the timeout — the captured PCM lives on TimeoutExpired.stdout and must
    # be returned, not discarded as b"" (the latent bug that made RX/TX always fail).
    def fake_run(argv, capture_output, timeout):
        raise selftest.subprocess.TimeoutExpired(argv, timeout, output=b"PCMDATA")

    monkeypatch.setattr(selftest.subprocess, "run", fake_run)
    assert selftest._capture(["gst-launch-1.0"], 0.1) == b"PCMDATA"


def test_run_audio_fails_cleanly_when_tx_spawn_raises():
    # A missing GStreamer plugin / gst-launch → spawn raises. Must return rc=1 (a clean
    # FAIL), not propagate a traceback out of the CLI.
    rate = 8000
    backend = FakeBackend({(1, "rx"): "oe5xrx.slot1", (1, "tx"): "oe5xrx.slot1.tx"}, cards={1: 7})

    def boom(argv):
        raise OSError("gst-launch-1.0 not found")

    rc = selftest.run_audio(
        slot=1,
        rate=rate,
        backend=backend,
        capture=lambda a, d: _pcm_s16(1000, rate, 1600),
        capture_tx=lambda a, d: _pcm_s16(1500, rate, 1600),
        spawn=boom,
        wait_running=lambda: None,
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
        capture_tx=lambda a, d: b"",
        spawn=lambda a: FakeProc(),
        wait_running=lambda: None,
        duration=0.2,
    )
    assert rc == 1


def test_run_audio_fails_when_no_reverse_tap_resolvable():
    # RX resolves + tone is present, but the backend can't map the slot to an ALSA card
    # (e.g. real HW with no aloop reverse cable) → TX self-check cannot run → FAIL, not false-pass.
    rate = 8000
    backend = FakeBackend({(1, "rx"): "oe5xrx.slot1", (1, "tx"): "oe5xrx.slot1.tx"}, cards={})
    rc = selftest.run_audio(
        slot=1,
        tx_freq=1500,
        rate=rate,
        backend=backend,
        capture=lambda a, d: _pcm_s16(1000, rate, 1600),
        capture_tx=lambda a, d: _pcm_s16(1500, rate, 1600),
        spawn=lambda a: FakeProc(),
        wait_running=lambda: None,
        duration=0.2,
    )
    assert rc == 1
