# tests/test_audio_fixtures.py
"""Pin the §5.7 golden contract fixtures shared by Session B (agent) and C (server).

Header/JSON assertions run everywhere. The Opus round-trip decode (1 kHz peak) is gated on
PyAV being importable — a dev-box/CI convenience, never a station_agent runtime dependency.
"""
import json
import pathlib
import struct

import pytest

from station_agent.audio import frame as F

FIX = pathlib.Path(__file__).parent / "fixtures" / "audio"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_advertise_fixture_matches_spec_5():
    adv = _load("advertise.json")
    assert adv["v"] == 1 and adv["type"] == "advertise"
    by_id = {s["stream_id"]: s for s in adv["streams"]}
    assert by_id["slot0.rx"]["format"] == {"rate": 8000, "channels": 1}
    assert by_id["slot0.rx"]["direction"] == "rx" and by_id["slot0.rx"]["codec"] == "opus"
    assert by_id["op.mic"]["slot"] is None
    assert by_id["op.mic"]["format"] == {"rate": 16000, "channels": 1}


def test_signaling_fixtures_have_versioned_envelope():
    for name in (
        "subscribe.json",
        "source_subscribe.json",
        "mic_open.json",
        "mic_state.json",
        "error_not_locked.json",
    ):
        msg = _load(name)
        assert msg["v"] == 1, name
        assert isinstance(msg["type"], str), name


def test_error_fixture_is_not_locked():
    err = _load("error_not_locked.json")
    assert err["type"] == "error" and err["code"] == "not_locked"


def test_media_frame_fixture_header_parses():
    data = (FIX / "media_frame_slot0rx.bin").read_bytes()
    mf = F.parse_frame(data)
    assert mf.stream_ref == 0  # slot0.rx
    assert mf.seq == 0 and mf.ts == 0
    assert len(mf.payload) > 0  # a real Opus packet


def test_media_frame_fixture_opus_decodes_to_1khz_peak():
    av = pytest.importorskip("av")
    from station_agent.audio.goertzel import dominant_bin

    data = (FIX / "media_frame_slot0rx.bin").read_bytes()
    payload = F.parse_frame(data).payload
    cc = av.CodecContext.create("libopus", "r")
    cc.sample_rate = 48000
    cc.format = "s16"
    cc.layout = "mono"
    samples = []
    rate = 48000
    for pkt in (av.Packet(payload), None):
        for fr in cc.decode(pkt):
            rate = fr.sample_rate
            pcm = bytes(fr.planes[0])[: fr.samples * 2]
            samples += list(struct.unpack("<%dh" % (len(pcm) // 2), pcm))
    assert samples, "opus payload decoded to no audio"
    assert dominant_bin(samples, [500, 1000, 1500, 2000, 3000], rate) == 1000
