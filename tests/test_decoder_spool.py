from station_agent.decoders.spool import Spool


def _seqs(s):
    return [f["seq"] for f in s.pending()]


def test_seq_monotonic_and_pending_order():
    s = Spool()
    frames = [s.add("aprs", 1, "ax25", {"i": i}, 1.0) for i in range(3)]
    assert [f["seq"] for f in frames] == [1, 2, 3]
    assert _seqs(s) == [1, 2, 3]
    assert frames[0]["type"] == "decoder_output" and frames[0]["reliable"] is True
    assert frames[0]["epoch"] == s.epoch


def test_epoch_default_unique_and_explicit():
    assert Spool().epoch != Spool().epoch
    assert Spool(epoch="A").epoch == "A"


def test_ack_removes_only_matching_epoch():
    s = Spool(epoch="A")
    s.add("aprs", 1, "ax25", {}, 1.0)
    s.add("aprs", 1, "ax25", {}, 1.0)
    assert s.ack("B", [1]) == 0 and len(s.pending()) == 2
    assert s.ack("A", [1, 99]) == 1 and _seqs(s) == [2]


def test_bound_by_items_drops_oldest():
    s = Spool(max_items=3)
    for i in range(5):
        s.add("aprs", 1, "ax25", {"i": i}, 0.0)
    assert _seqs(s) == [3, 4, 5] and s.dropped == 2


def test_bound_by_bytes_drops_oldest():
    s = Spool(max_bytes=700)
    for i in range(5):
        s.add("aprs", 1, "ax25", {"blob": "x" * 200}, 0.0)
    assert 1 <= len(s.pending()) < 5
    assert s.dropped == 5 - len(s.pending())
    assert _seqs(s)[-1] == 5


def test_seq_continues_after_ack():
    s = Spool(epoch="A")
    s.add("aprs", 1, "ax25", {}, 0.0)
    s.ack("A", [1])
    assert s.add("aprs", 1, "ax25", {}, 0.0)["seq"] == 2


def test_oversized_single_frame_is_dropped_not_stuck():
    s = Spool(max_bytes=50)
    f = s.add("aprs", 1, "ax25", {"blob": "x" * 500}, 0.0)
    assert f["seq"] == 1 and s.pending() == [] and s.dropped == 1
