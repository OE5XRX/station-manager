"""Per-stream Opus bridge over gst-launch subprocesses (Spec 0 §5.4, §10 minimal path).

Each direction is one ``gst-launch-1.0`` pipeline. Discrete 20 ms Opus packets cross the
Python↔GStreamer boundary as RTP over UDP loopback — without an ``appsink`` (needs
python-gi, which the A-image does not ship) a UDP datagram is the cheapest self-delimiting
boundary that yields exactly one packet per read:

    RX (tap module → WS):   pipewiresrc(target=rx_node) ! opusenc(FEC,VBR,VOIP,DTX,20ms)
                            ! rtpopuspay ! udpsink        → agent strip_rtp → on_opus()
    TX (WS → inject module): agent feed_opus → wrap_rtp → udpsink(to gst)
                            → udpsrc ! rtpjitterbuffer ! rtpopusdepay ! opusdec(PLC,FEC)
                            ! [DSP chain: band-pass/gate/comp/makeup, optional pre-limiter
                              meter tap] ! limiter ! pipewiresink(target=tx_node)

The pipeline argv builders are pure and unit-tested; the process/socket lifecycle uses
injected ``spawn``/``socket_factory`` seams so tests need no GStreamer, PipeWire, or real
sockets. On real HW/sim the defaults spawn the tools shipped in the A-image.
"""

from __future__ import annotations

import dataclasses
import logging
import socket
import subprocess
import threading

from station_agent.audio import rtp, tx_meter
from station_agent.audio.tx_dsp import (
    DSP_ELEMENTS,
    TxDspConfig,
    limiter_fragment,
    pre_limiter_fragment,
)

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
        "audio-type=voice",
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


def build_tx_argv(
    tx_node: str,
    port: int,
    rate: int,
    *,
    dsp: TxDspConfig | None = None,
    meter: bool = False,
) -> list[str]:
    """gst-launch pipeline: UDP ``port`` → RTP jitter buffer → Opus decode (PLC + FEC)
    → resample → [DSP chain + meter tap] → inject into ``tx_node``.

    With ``dsp=None, meter=False`` the argv is the plain pass-through pipeline. ``dsp``
    inserts the F32 band-pass/gate/compressor/makeup chain and, as the LAST stage before
    the sink, the limiter. ``meter`` adds a leaky pre-limiter tee branch (F32 on stdout).
    ``meter=True`` with ``dsp=None`` yields F32 + tee only (no DSP chain, no limiter)."""
    # No surrounding quotes: this argv element is passed straight to Popen (shell=False), so
    # embedded quotes would be literal and break GStreamer's caps parse. The comma-separated
    # caps string is a single argv token — no shell word-splitting to protect against.
    caps = f"application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload={_RTP_PT}"
    head = [
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
        f"caps={caps}",
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
    ]
    sink = ["pipewiresink", f"target-object={tx_node}", "sync=false"]
    if dsp is None and not meter:
        return [*head, "!", f"audio/x-raw,rate={rate},channels=1", "!", *sink]

    argv = [*head, "!", f"audio/x-raw,format=F32LE,rate={rate},channels=1"]
    if dsp is not None:
        argv += pre_limiter_fragment(dsp)
    if meter:
        argv += ["!", "tee", "name=txm", "!", "queue"]
    if dsp is not None:
        argv += limiter_fragment(dsp)
    argv += ["!", "audioconvert", "!", *sink]
    if meter:
        argv += [
            "txm.",
            "!",
            "queue",
            "leaky=downstream",
            "max-size-buffers=8",
            "!",
            "fdsink",
            "fd=1",
            "sync=false",
        ]
    return argv


def _default_spawn(argv: list[str]):
    # Output goes to UDP; we don't read stdout. DEVNULL keeps the pipe from filling.
    return subprocess.Popen(  # noqa: S603 — argv is fixed tool + resolved node/port
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _spawn_with_stdout(argv: list[str]):
    # Meter tap: F32 PCM on stdout. stderr stays DEVNULL (gst -q only prints errors).
    return subprocess.Popen(  # noqa: S603 — argv is fixed tool + resolved node/port
        argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )


def _gst_inspect_exists(name: str) -> bool:
    return (
        subprocess.run(  # noqa: S603 — fixed tool name, element name from a constant
            ["gst-inspect-1.0", "--exists", name],  # noqa: S607
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        ).returncode
        == 0
    )


_probe_result: bool | None = None  # memo for the default probe; definitive results only


def _reset_probe_cache() -> None:
    global _probe_result
    _probe_result = None


def probe_dsp_available(inspect=None) -> bool:
    """True iff every DSP element exists.

    BLOCKING (spawns gst-inspect-1.0): call from a worker thread, never the asyncio loop.
    The default probe memoises only DEFINITIVE answers (the image does not change at
    runtime); a timeout/OSError returns False for this call but is not cached, so a cold
    boot hiccup cannot leave the station without its limiter until restart. An injected
    ``inspect`` (tests) is never memoised."""
    global _probe_result
    if inspect is None:
        if _probe_result is not None:
            return _probe_result
        check = _gst_inspect_exists
    else:
        check = inspect
    try:
        result = all(check(el) for el in DSP_ELEMENTS)
    except (OSError, subprocess.SubprocessError):
        return False
    if inspect is None:
        _probe_result = result
    return result


def _read_exact(stream, size: int) -> bytes:
    """Read ``size`` bytes; returns b"" only at EOF (a short final chunk is dropped)."""
    chunks, got = [], 0
    while got < size:
        b = stream.read(size - got)
        if not b:
            return b""
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


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
        spawn=None,
        socket_factory=_udp_socket,
        ssrc: int = 0x5852_5841,  # "XRXA"
        dsp: TxDspConfig | None = None,
        on_meter=None,
        start_reader: bool = True,
        startup_grace: float = 0.3,
        dsp_probe=None,
    ):
        self._node = tx_node
        self._port = port
        self._rate = rate
        self._spawn = spawn
        self._socket_factory = socket_factory
        self._ssrc = ssrc
        self._dsp = dsp
        self._on_meter = on_meter
        self._start_reader = start_reader
        self._startup_grace = startup_grace
        self._dsp_probe = dsp_probe or probe_dsp_available
        self.dsp_mode = "off"  # "off" | "full" | "degraded" | "failed"
        self.ceiling_dbfs: float | None = dsp.ceiling_dbfs if dsp is not None else None
        self._reader: threading.Thread | None = None
        self._proc = None
        self._sock = None
        self._seq = 0
        self._ts = 0

    def start(self) -> None:
        self._sock = self._socket_factory()
        meter = self._on_meter is not None
        spawn = self._spawn or (_spawn_with_stdout if meter else _default_spawn)
        cfg = self._dsp
        if cfg is not None and cfg.enabled and not self._dsp_probe():
            # Probe runs here (worker thread via _to_thread), never on the WS loop.
            self._dsp = cfg = dataclasses.replace(cfg, enabled=False)
        self._proc = spawn(build_tx_argv(self._node, self._port, self._rate, dsp=cfg, meter=meter))
        if cfg is None:
            self.dsp_mode = "off"
        elif not cfg.enabled:
            self.dsp_mode = "degraded"
        elif self._died_at_startup(self._proc):
            # Element present but the pipeline failed to construct/link: TX must never
            # break entirely (spec §3.2) -> plain pass-through, surfaced via the meter.
            logger.warning("tx-bridge: DSP pipeline exited at startup; falling back")
            self._dsp = cfg = dataclasses.replace(cfg, enabled=False)
            self._close_stdout(self._proc)  # don't leak the dead process's pipe fd
            self._proc = spawn(
                build_tx_argv(self._node, self._port, self._rate, dsp=cfg, meter=meter)
            )
            self.dsp_mode = "degraded"
            if self._died_at_startup(self._proc):
                logger.error("tx-bridge: TX pipeline failed even without DSP")
                self.dsp_mode = "failed"
        else:
            self.dsp_mode = "full"
        stdout = getattr(self._proc, "stdout", None)
        if meter and self._start_reader and stdout is not None and self.dsp_mode != "failed":
            threshold = cfg.limiter_threshold if (cfg is not None and cfg.enabled) else None
            # N:1 policy value (NOT the gst-native 1/N emitted in the argv); see tx_meter.
            ratio = cfg.policy.limiter_ratio if cfg is not None else 1.0
            ceiling = cfg.ceiling_dbfs if cfg is not None else None
            # Everything the thread needs is passed in: self._proc is None after stop().
            self._reader = threading.Thread(
                target=self._meter_loop,
                args=(stdout, threshold, ratio, self.dsp_mode, ceiling),
                name=f"tx-meter-{self._port}",
                daemon=True,
            )
            self._reader.start()

    @staticmethod
    def _close_stdout(proc) -> None:
        stdout = getattr(proc, "stdout", None)
        if stdout is not None:
            try:
                stdout.close()
            except (OSError, ValueError):
                pass

    def _died_at_startup(self, proc) -> bool:
        try:
            proc.wait(timeout=self._startup_grace)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _meter_loop(self, stream, threshold, ratio, dsp_mode, ceiling) -> None:
        size = tx_meter.chunk_bytes(self._rate)
        while True:
            try:
                buf = _read_exact(stream, size)
            except (OSError, ValueError):
                return  # pipe closed on stop()
            if not buf:
                return
            reading = tx_meter.compute_meter(buf, limiter_threshold=threshold, limiter_ratio=ratio)
            reading["dsp"] = dsp_mode
            reading["ceiling_dbfs"] = ceiling
            try:
                self._on_meter(reading)
            except Exception:  # noqa: BLE001 — a consumer error must not kill the meter
                logger.exception("tx-bridge: on_meter callback raised")

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
        proc = self._proc
        _terminate(proc)  # process death gives the reader EOF
        self._proc = None
        if self._reader is not None:
            self._reader.join(timeout=_STOP_WAIT)
            self._reader = None
        self._close_stdout(proc)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
