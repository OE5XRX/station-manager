"""Per-stream Opus bridge over gst-launch subprocesses (Spec 0 §5.4, §10 minimal path).

Each direction is one ``gst-launch-1.0`` pipeline. Discrete 20 ms Opus packets cross the
Python↔GStreamer boundary as RTP over UDP loopback — without an ``appsink`` (needs
python-gi, which the A-image does not ship) a UDP datagram is the cheapest self-delimiting
boundary that yields exactly one packet per read:

    RX (tap module → WS):   pipewiresrc(target=rx_node) ! opusenc(FEC,VBR,VOIP,DTX,20ms)
                            ! rtpopuspay ! udpsink        → agent strip_rtp → on_opus()
    TX (WS → inject module): agent feed_opus → wrap_rtp → udpsink(to gst)
                            → udpsrc ! rtpjitterbuffer ! rtpopusdepay ! opusdec(PLC,FEC)
                            ! pipewiresink(target=tx_node)

The pipeline argv builders are pure and unit-tested; the process/socket lifecycle uses
injected ``spawn``/``socket_factory`` seams so tests need no GStreamer, PipeWire, or real
sockets. On real HW/sim the defaults spawn the tools shipped in the A-image.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import threading

from station_agent.audio import rtp

logger = logging.getLogger(__name__)

# Opus RTP uses a fixed 48 kHz clock (RFC 7587) regardless of the media sample rate, so a
# 20 ms frame advances the RTP timestamp by 960 ticks.
RTP_TS_PER_FRAME = 960
_LOOPBACK = "127.0.0.1"
_RTP_PT = 96
_STOP_WAIT = 3.0


def build_rx_argv(rx_node: str, port: int, rate: int) -> list[str]:
    """gst-launch pipeline: tap ``rx_node`` → Opus (20 ms, VBR, VOIP, in-band FEC, DTX)
    → RTP → UDP ``port`` on loopback."""
    return [
        "gst-launch-1.0",
        "-q",
        "pipewiresrc",
        f"target-object={rx_node}",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        f"audio/x-raw,rate={rate},channels=1",
        "!",
        "opusenc",
        "bitrate-type=vbr",
        "audio-type=voip",
        "frame-size=20",
        "inband-fec=true",
        "dtx=true",
        "!",
        "rtpopuspay",
        f"pt={_RTP_PT}",
        "!",
        "udpsink",
        f"host={_LOOPBACK}",
        f"port={port}",
        "sync=false",
    ]


def build_tx_argv(tx_node: str, port: int, rate: int) -> list[str]:
    """gst-launch pipeline: UDP ``port`` → RTP jitter buffer → Opus decode (PLC + FEC)
    → resample → inject into ``tx_node``."""
    caps = f"application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload={_RTP_PT}"
    return [
        "gst-launch-1.0",
        "-q",
        "udpsrc",
        # SECURITY: bind the transmitter's RTP receive socket to loopback ONLY. udpsrc
        # defaults address to 0.0.0.0 (all interfaces), which would let any host on the
        # network inject Opus RTP straight into the module TX sink — i.e. spoof audio onto
        # a keyed amateur transmitter. The producer is always the local agent (feed_opus →
        # 127.0.0.1), so loopback is correct and closes the remote-injection vector.
        f"address={_LOOPBACK}",
        f"port={port}",
        f'caps="{caps}"',
        "!",
        "rtpjitterbuffer",
        "!",
        "rtpopusdepay",
        "!",
        "opusdec",
        "plc=true",
        "use-inband-fec=true",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        f"audio/x-raw,rate={rate},channels=1",
        "!",
        "pipewiresink",
        f"target-object={tx_node}",
        "sync=false",
    ]


def _default_spawn(argv: list[str]):
    # Output goes to UDP; we don't read stdout. DEVNULL keeps the pipe from filling.
    return subprocess.Popen(  # noqa: S603 — argv is fixed tool + resolved node/port
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _udp_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


class PortAllocator:
    """Hands out distinct UDP ports for concurrent bridges; reuses released ports.

    Thread-safe: bridges are created on the WS loop thread today, but a lock keeps this
    correct if a future caller acquires off-thread.
    """

    def __init__(self, base: int = 47000):
        self._base = base
        self._in_use: set[int] = set()
        self._lock = threading.Lock()

    def acquire(self) -> int:
        with self._lock:
            port = self._base
            while port in self._in_use:
                port += 1
            self._in_use.add(port)
            return port

    def release(self, port: int) -> None:
        with self._lock:
            self._in_use.discard(port)


def _terminate(proc) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=_STOP_WAIT)
            except subprocess.TimeoutExpired:
                proc.kill()
    except (OSError, ValueError) as exc:
        logger.debug("bridge: terminate failed: %s", exc)


class RxBridge:
    """Taps a PipeWire source node, Opus-encodes it, and calls ``on_opus(payload)`` for
    each 20 ms packet. The reader runs in a daemon thread (like the control client's own
    loop); ``on_opus`` is invoked from that thread."""

    def __init__(
        self,
        rx_node: str,
        port: int,
        rate: int,
        on_opus,
        *,
        spawn=_default_spawn,
        socket_factory=_udp_socket,
        start_reader: bool = True,
    ):
        self._node = rx_node
        self._port = port
        self._rate = rate
        self._on_opus = on_opus
        self._spawn = spawn
        self._socket_factory = socket_factory
        self._start_reader = start_reader
        self._proc = None
        self._sock = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = self._socket_factory()
        self._sock.bind((_LOOPBACK, self._port))
        self._proc = self._spawn(build_rx_argv(self._node, self._port, self._rate))
        if self._start_reader:
            self._reader = threading.Thread(
                target=self._read_loop, name=f"rx-bridge-{self._port}", daemon=True
            )
            self._reader.start()

    def _read_loop(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                datagram, _addr = self._sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                return  # socket closed on stop()
            self._handle_datagram(datagram)

    def _handle_datagram(self, datagram: bytes) -> None:
        try:
            payload = rtp.strip_rtp(datagram)
        except rtp.RtpError:
            logger.debug("rx-bridge: dropping malformed RTP datagram")
            return
        try:
            self._on_opus(payload)
        except Exception:  # noqa: BLE001 — a consumer error must not kill the reader
            logger.exception("rx-bridge: on_opus callback raised")

    def stop(self) -> None:
        self._stop.set()
        _terminate(self._proc)
        self._proc = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._reader is not None:
            self._reader.join(timeout=_STOP_WAIT)
            self._reader = None


class TxBridge:
    """Decodes Opus fed via ``feed_opus`` and injects it into a PipeWire sink node.

    ``feed_opus`` wraps each packet in RTP (own seq/ts/ssrc) and sends it to the gst
    ``udpsrc``; the pipeline's jitter buffer + PLC + in-band FEC give the loss tolerance
    Spec 0 §2/§5.4 require.
    """

    def __init__(
        self,
        tx_node: str,
        port: int,
        rate: int,
        *,
        spawn=_default_spawn,
        socket_factory=_udp_socket,
        ssrc: int = 0x5852_5841,  # "XRXA"
    ):
        self._node = tx_node
        self._port = port
        self._rate = rate
        self._spawn = spawn
        self._socket_factory = socket_factory
        self._ssrc = ssrc
        self._proc = None
        self._sock = None
        self._seq = 0
        self._ts = 0

    def start(self) -> None:
        self._sock = self._socket_factory()
        self._proc = self._spawn(build_tx_argv(self._node, self._port, self._rate))

    def feed_opus(self, payload: bytes) -> None:
        if self._sock is None:
            logger.debug("tx-bridge: feed before start; dropping")
            return
        datagram = rtp.wrap_rtp(payload, seq=self._seq, ts=self._ts, ssrc=self._ssrc, pt=_RTP_PT)
        try:
            self._sock.sendto(datagram, (_LOOPBACK, self._port))
        except OSError as exc:
            logger.debug("tx-bridge: sendto failed: %s", exc)
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + RTP_TS_PER_FRAME) & 0xFFFFFFFF

    def stop(self) -> None:
        _terminate(self._proc)
        self._proc = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
