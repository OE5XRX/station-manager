# tests/test_audio_opus_bridge.py
"""Opus bridge: gst-launch pipeline argv builders + RTP/UDP datagram seams."""

from station_agent.audio import opus_bridge as ob
from station_agent.audio import rtp


def test_build_rx_argv_has_opus_profile_and_target():
    argv = ob.build_rx_argv("oe5xrx.slot1", 47001, 8000)
    joined = " ".join(argv)
    assert argv[0] == "gst-launch-1.0"
    assert "pipewiresrc" in joined and "target-object=oe5xrx.slot1" in joined
    assert "opusenc" in joined
    # audio-type must be a VALID GstOpusEncAudioType nick (generic/voice/restricted-lowdelay);
    # "voip" is NOT valid and makes gst-launch reject the whole pipeline (Session E finding).
    assert "audio-type=voice" in joined and "audio-type=voip" not in joined
    assert "inband-fec=true" in joined  # §5.4 FEC on
    assert "rate=8000" in joined
    assert "rtpopuspay" in joined
    assert "udpsink" in joined and "port=47001" in joined and "host=127.0.0.1" in joined


def test_build_rx_argv_pins_stream_non_live_and_never_syncs():
    """pipewiresrc start race (PipeWire <= 1.6.11): a live stream whose CLOCK_LOST re-PLAY
    shifts base_time makes basesrc hold every buffer, and the 2-buffer stream then drops a
    graph cycle in every 4-6 for its whole life. stream.is-live=false = no basesrc sync."""
    argv = ob.build_rx_argv("oe5xrx.slot1", 47001, 8000)
    props = next(a for a in argv if a.startswith("stream-properties="))
    assert "stream.is-live=false" in props.split(",")
    # still pinned to its node (no relink to another slot's audio)
    assert "node.dont-reconnect=true" in props and "node.dont-fallback=true" in props
    # a non-live pipeline must never sync the sink: it would wait on raw PipeWire PTS
    sink = argv[argv.index("udpsink") :]
    assert "sync=false" in sink and "sync=true" not in argv


def test_build_tx_argv_keeps_live_stream_properties():
    """The race lives in pipewiresrc; the TX pipewiresink stays as it was."""
    argv = ob.build_tx_argv("oe5xrx.slot1.tx", 47002, 8000)
    props = next(a for a in argv if a.startswith("stream-properties="))
    assert "stream.is-live=false" not in props


def test_build_tx_argv_has_jitterbuffer_plc_and_target():
    argv = ob.build_tx_argv("oe5xrx.slot1.tx", 47002, 8000)
    joined = " ".join(argv)
    assert "udpsrc" in joined and "port=47002" in joined
    # SECURITY: the transmitter's RTP receive socket must bind loopback only, never 0.0.0.0
    assert "address=127.0.0.1" in joined
    assert "rtpjitterbuffer" in joined  # §2 adaptive jitter buffer
    assert "rtpopusdepay" in joined
    assert "opusdec" in joined and "plc=true" in joined  # §5.4 PLC on
    assert "pipewiresink" in joined and "target-object=oe5xrx.slot1.tx" in joined
    # caps must be a bare token (no embedded quotes) — argv is passed shell=False, so
    # surrounding quotes would be literal and break GStreamer's caps parse.
    caps_arg = next(a for a in argv if a.startswith("caps="))
    assert '"' not in caps_arg and caps_arg.startswith("caps=application/x-rtp")


class FakeProc:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class FakeSock:
    def __init__(self, datagrams=None):
        self._in = list(datagrams or [])
        self.sent = []
        self.closed = False
        self.bound = None

    def bind(self, addr):
        self.bound = addr

    def sendto(self, data, addr):
        self.sent.append((bytes(data), addr))

    def close(self):
        self.closed = True


def test_rx_bridge_start_spawns_pipeline_and_binds_socket():
    spawned = {}

    def spawn(argv):
        spawned["argv"] = argv
        return FakeProc()

    sock = FakeSock()
    got = []
    br = ob.RxBridge(
        "oe5xrx.slot1",
        47003,
        8000,
        on_opus=lambda p, ts: got.append((p, ts)),
        spawn=spawn,
        socket_factory=lambda: sock,
        start_reader=False,
    )
    br.start()
    assert spawned["argv"][0] == "gst-launch-1.0"
    assert sock.bound == ("127.0.0.1", 47003)
    # a received RTP datagram yields its Opus payload
    dg = rtp.wrap_rtp(b"opus-bytes", seq=1, ts=960, ssrc=1, pt=96)
    br._handle_datagram(dg)
    # …together with its RTP timestamp (48 kHz Opus clock), which also feeds the health
    assert got == [(b"opus-bytes", 960)]
    assert br.health.packets == 1
    br.stop()
    assert sock.closed is True


def test_rx_bridge_ignores_malformed_datagram():
    got = []
    br = ob.RxBridge(
        "n",
        1,
        8000,
        on_opus=lambda p, ts: got.append(p),
        spawn=lambda a: FakeProc(),
        socket_factory=FakeSock,
        start_reader=False,
    )
    br._handle_datagram(b"\x00\x01")  # too short → RtpError swallowed
    assert got == []
    assert br.health.packets == 0


def test_rx_bridge_health_counts_packet_even_if_consumer_raises():
    def boom(p, ts):
        raise RuntimeError("consumer bug")

    br = ob.RxBridge(
        "n",
        1,
        8000,
        on_opus=boom,
        spawn=lambda a: FakeProc(),
        socket_factory=FakeSock,
        start_reader=False,
    )
    br._handle_datagram(rtp.wrap_rtp(b"x" * 40, seq=1, ts=1920, ssrc=1, pt=96))
    assert br.health.packets == 1


def test_rx_bridge_health_starts_fresh_on_start():
    """The stall clock runs from start(), not from construction (a slow bind/spawn)."""
    t = [0.0]
    br = ob.RxBridge(
        "n",
        1,
        8000,
        on_opus=lambda p, ts: None,
        spawn=lambda a: FakeProc(),
        socket_factory=FakeSock,
        start_reader=False,
        clock=lambda: t[0],
    )
    t[0] = 50.0
    br.start()
    t[0] = 55.0
    assert br.health.verdict() is None
    t[0] = 61.0
    assert "no RTP packet" in br.health.verdict()


def test_tx_bridge_feed_wraps_rtp_and_sends():
    sock = FakeSock()
    br = ob.TxBridge(
        "oe5xrx.slot1.tx",
        47004,
        8000,
        spawn=lambda a: FakeProc(),
        socket_factory=lambda: sock,
        ssrc=0x1234,
    )
    br.start()
    br.feed_opus(b"packet-one")
    br.feed_opus(b"packet-two")
    assert len(sock.sent) == 2
    # each datagram is valid RTP carrying the fed payload; seq increments
    d0, _ = sock.sent[0]
    d1, _ = sock.sent[1]
    assert rtp.strip_rtp(d0) == b"packet-one"
    assert rtp.strip_rtp(d1) == b"packet-two"
    import struct

    seq0 = struct.unpack("!H", d0[2:4])[0]
    seq1 = struct.unpack("!H", d1[2:4])[0]
    assert seq1 == (seq0 + 1) & 0xFFFF
    br.stop()
    assert sock.closed is True


def test_tx_bridge_timestamp_advances_by_frame():
    sock = FakeSock()
    br = ob.TxBridge(
        "n.tx", 5, 8000, spawn=lambda a: FakeProc(), socket_factory=lambda: sock, ssrc=1
    )
    br.start()
    br.feed_opus(b"a")
    br.feed_opus(b"b")
    import struct

    ts0 = struct.unpack("!I", sock.sent[0][0][4:8])[0]
    ts1 = struct.unpack("!I", sock.sent[1][0][4:8])[0]
    assert ts1 - ts0 == ob.RTP_TS_PER_FRAME  # 960 @ 48k RTP clock per 20ms


def test_port_allocator_assigns_distinct_ports():
    alloc = ob.PortAllocator(base=47000)
    a = alloc.acquire()
    b = alloc.acquire()
    assert a != b
    alloc.release(a)
    assert alloc.acquire() == a  # released port is reused


def test_tx_bridge_is_alive_tracks_process_lifecycle():
    proc = FakeProc()
    br = ob.TxBridge("n.tx", 47004, 8000, spawn=lambda a: proc, socket_factory=lambda: FakeSock())
    assert br.is_alive() is False  # before start
    br.start()
    assert br.is_alive() is True
    proc.poll = lambda: 1  # gst process exited
    assert br.is_alive() is False
    br.stop()
    assert br.is_alive() is False  # after stop
