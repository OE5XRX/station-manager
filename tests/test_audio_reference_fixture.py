from apps.audio import diagnostics as sd
from station_agent.audio.frame import parse_frame


def test_fixture_loads_nonempty_opus_packets():
    frames = sd.load_reference_frames()
    assert len(frames) >= 10  # ~200ms+ of 20ms packets
    assert all(isinstance(f, bytes) and len(f) > 0 for f in frames)


def test_iter_media_frames_are_valid_s5_3_frames():
    frames = sd.load_reference_frames()
    out = list(sd.iter_media_frames(frames[:5], stream_ref=7, repeat=2))
    assert len(out) == 10
    seqs = []
    for data in out:
        mf = parse_frame(data)
        assert mf.stream_ref == 7
        seqs.append(mf.seq)
    assert seqs == sorted(seqs)  # monotonic seq across the repeat
