"""Audio-path diagnostics: calibrated reference generation, dBFS metering, and
measured inject/tap pipelines for the TX chain (Spec: audio-path-diagnostics).

Pure DSP + argv builders are unit-tested; the subprocess capture/spawn seams are
injected (mirrors ``selftest.py``) so CI needs no GStreamer/PipeWire.

RF SAFETY: nothing here keys the SA818. Inject/measure end at the digital
ALSA/UAC2 edge (D); carrier keying is a separate control-plane action.
"""

from __future__ import annotations

import dataclasses
import logging as _logging
import math
import os as _os
import re as _re
import socket
import struct
import subprocess as _subprocess
import threading

from station_agent.audio import rtp as _rtp
from station_agent.audio import selftest as _selftest
from station_agent.audio.opus_bridge import RTP_TS_PER_FRAME, probe_dsp_available
from station_agent.audio.tx_dsp import limiter_fragment, pre_limiter_fragment

_log = _logging.getLogger(__name__)

FULL_SCALE_S16 = 32767

REF_FREQ_HZ = 1000
REF_LEVEL_DBFS = -20.0
REF_WINDOW_MS = 300
REF_SETTLE_MS = 200

MAX_DURATION_MS = 5000

#: Dedicated stream_ref for server-originated U-anchor reference frames. A high
#: sentinel so it can never collide with a real slotN.rx / op.mic ref (small,
#: assigned ascending from 0). The engine routes only this ref to the diagnostic
#: bridge; it is never a live TX stream.
DIAG_STREAM_REF: int = 0xFFFE


def rms_peak_dbfs(pcm: bytes) -> tuple[float | None, float | None, bool]:
    n = len(pcm) // 2
    if n == 0:
        return (None, None, True)
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    peak = max(abs(s) for s in samples)
    sumsq = sum(s * s for s in samples)
    rms = math.sqrt(sumsq / n)
    if peak == 0 or rms == 0:
        return (None, None, True)
    rms_dbfs = 20 * math.log10(rms / FULL_SCALE_S16)
    peak_dbfs = 20 * math.log10(peak / FULL_SCALE_S16)
    return (round(rms_dbfs, 2), round(peak_dbfs, 2), False)


def generate_sine_pcm(freq_hz: int, level_dbfs: float, duration_ms: int, rate: int) -> bytes:
    n = int(rate * duration_ms / 1000)
    amp = (10 ** (level_dbfs / 20)) * FULL_SCALE_S16
    out = [
        max(-32768, min(32767, int(round(amp * math.sin(2 * math.pi * freq_hz * i / rate)))))
        for i in range(n)
    ]
    return struct.pack(f"<{n}h", *out)


MEAS_FD = 3
_RTP_PT = 96
_LOOPBACK = "127.0.0.1"


def build_measured_inject_argv(
    tx_node: str, freq_hz: int, level_dbfs: float, rate: int, *, meas_fd: int = MEAS_FD
) -> list[str]:
    """Build a gst-launch argv that injects a calibrated sine into *tx_node* and taps PCM.

    ``meas_fd`` is the file descriptor the child will write raw S16LE samples to.  The
    caller must open an OS pipe, pass the write-end fd as *meas_fd* **and** include it in
    ``pass_fds`` when spawning, so the child actually inherits it.  Hardcoding fd=3 is wrong
    because ``pass_fds`` preserves the fd at its current number, which is usually >3.

    BUG2 fix: ``audiotestsrc volume=1.0`` so the dedicated ``volume`` element is the sole
    gain stage (audiotestsrc default 0.8 would compound with the volume element and yield
    −21.94 dBFS instead of the requested level).
    """
    linear = 10 ** (level_dbfs / 20)
    return [
        "gst-launch-1.0",
        "-q",
        "audiotestsrc",
        "is-live=true",
        "wave=sine",
        f"freq={freq_hz}",
        "volume=1.0",
        "!",
        f"audio/x-raw,rate={rate},channels=1",
        "!",
        "audioconvert",
        "!",
        "volume",
        f"volume={linear:g}",
        "!",
        "opusenc",
        "audio-type=voice",
        "frame-size=20",
        "inband-fec=true",
        "!",
        "opusdec",
        "plc=true",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        f"audio/x-raw,rate={rate},channels=1",
        "!",
        "tee",
        "name=t",
        "t.",
        "!",
        "queue",
        "!",
        "pipewiresink",
        f"target-object={tx_node}",
        "sync=false",
        "t.",
        "!",
        "queue",
        "!",
        "audioconvert",
        "!",
        f"audio/x-raw,format=S16LE,rate={rate},channels=1",
        "!",
        "fdsink",
        f"fd={meas_fd}",
    ]


def build_measured_tx_argv(
    tx_node: str, port: int, rate: int, *, meas_fd: int = MEAS_FD, dsp=None
) -> list[str]:
    """Build a gst-launch argv that taps a UDP RTP stream into *tx_node* and measures PCM.

    ``meas_fd`` is the pipe write-end fd the child will write to; see
    :func:`build_measured_inject_argv` for the rationale.

    With ``dsp`` (a :class:`TxDspConfig`) the production TX DSP chain (band-pass/gate/
    compressor/makeup + final limiter) sits before the tee, so both the sink and the
    measurement tap see the post-limiter (D) signal.
    """
    caps = f"application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload={_RTP_PT}"
    dsp_args: list[str] = []
    if dsp is not None:
        dsp_args = [
            "!",
            f"audio/x-raw,format=F32LE,rate={rate},channels=1",
            *pre_limiter_fragment(dsp),
            *limiter_fragment(dsp),
        ]
    return [
        "gst-launch-1.0",
        "-q",
        "udpsrc",
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
        "!",
        f"audio/x-raw,rate={rate},channels=1",
        *dsp_args,
        "!",
        "tee",
        "name=t",
        "t.",
        "!",
        "queue",
        "!",
        "pipewiresink",
        f"target-object={tx_node}",
        "sync=false",
        "t.",
        "!",
        "queue",
        "!",
        "audioconvert",
        "!",
        f"audio/x-raw,format=S16LE,rate={rate},channels=1",
        "!",
        "fdsink",
        f"fd={meas_fd}",
    ]


def build_reverse_tap_argv(tap: str, rate: int, duration: float) -> list[str]:
    return _selftest.build_tx_capture_argv(tap, rate, duration)


_WPCTL_VOL = _re.compile(r"Volume:\s*([0-9]+\.[0-9]+)")
_WPCTL_MUTED = _re.compile(r"\[MUTED\]", _re.IGNORECASE)


def parse_wpctl_volume(text: str) -> float | None:
    """Parse the linear volume from ``wpctl get-volume`` output.

    Returns ``0.0`` if the output contains ``[MUTED]`` (muted sink → effectively
    zero gain for diagnostic purposes; 20log10(0) is −∞ dB, handled downstream by
    the ``linear > 0`` guard in ``collect_static_gains``).
    """
    if not text:
        return None
    if _WPCTL_MUTED.search(text):
        return 0.0
    m = _WPCTL_VOL.search(text)
    return float(m.group(1)) if m else None


def collect_static_gains(backend, slot: int) -> dict:
    sink = getattr(backend, "tx_sink_node", lambda s: None)(slot)
    linear = backend.get_volume(sink) if sink else None
    db = 20 * math.log10(linear) if linear and linear > 0 else None
    return {
        "sink_volume_linear": linear,
        "sink_volume_db": round(db, 2) if db is not None else None,
        "resample": "48k<->8k",
        "note": "sink_volume_db is the C->D static gain stage (spec: sink vol 0.40 ~= -8 dB)",
    }


def _default_spawn(make_argv):
    """Spawn the measurement pipeline.

    ``make_argv(meas_fd: int) -> list[str]`` is called with the actual write-end fd
    so the child's argv embeds the real fd number (not the hardcoded constant 3).
    ``pass_fds`` passes it to the child; the parent's copy is closed right after.

    Returns ``(proc, read_fd)`` where ``read_fd`` is the pipe read-end.

    Both ``r`` and ``w`` are closed if ``make_argv`` or ``Popen`` raises so that
    repeated failures on a long-running agent do not exhaust file descriptors.
    """
    r, w = _os.pipe()
    try:
        argv = make_argv(w)
        proc = _subprocess.Popen(  # noqa: S603 — fixed tool + resolved node
            argv,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            pass_fds=(w,),
        )
    except Exception:
        try:
            _os.close(r)
        except OSError:
            pass
        try:
            _os.close(w)
        except OSError:
            pass
        raise
    _os.close(w)
    return proc, r


def _default_read_measfd(read_fd: int, nbytes: int, timeout: float) -> bytes:
    """Read up to *nbytes* raw bytes from *read_fd* within *timeout* seconds, then close it."""
    import select
    import time as _t

    buf = bytearray()
    deadline = _t.monotonic() + timeout
    while len(buf) < nbytes and _t.monotonic() < deadline:
        r, _, _ = select.select([read_fd], [], [], max(0.0, deadline - _t.monotonic()))
        if not r:
            break
        chunk = _os.read(read_fd, nbytes - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    try:
        _os.close(read_fd)
    except OSError:
        pass
    return bytes(buf)


def _tap(point, rate, rms, peak, silent, window_ms, computed, note=None):
    d = {
        "point": point,
        "format": {"rate": rate, "channels": 1},
        "rms_dbfs": rms,
        "peak_dbfs": peak,
        "window_ms": window_ms,
        "silent": silent,
        "computed": computed,
    }
    if note is not None:
        d["note"] = note
    return d


def build_cd_taps(c_rms, c_peak, c_silent, gains: dict, rate: int, window_ms: int) -> list[dict]:
    """Build the [C, D] tap list from a measured C plus the static sink gain.

    C is the real measurement (``computed=False``); D is projected D = C + sink
    volume (``computed=True``) per spec §3. Mirrors the rules previously inline in
    ``run_diagnostic`` so anchor C and anchor U produce an identical schema.
    """
    taps = [_tap("C", rate, c_rms, c_peak, c_silent, window_ms, computed=False)]
    linear = gains.get("sink_volume_linear")
    sink_db = gains.get("sink_volume_db")
    if c_silent:
        taps.append(_tap("D", rate, None, None, True, window_ms, computed=True))
    elif linear == 0:
        taps.append(_tap("D", rate, None, None, True, window_ms, computed=True, note="sink muted"))
    elif sink_db is not None:
        taps.append(
            _tap(
                "D",
                rate,
                round(c_rms + sink_db, 2),
                round(c_peak + sink_db, 2),
                False,
                window_ms,
                computed=True,
            )
        )
    else:
        taps.append(
            _tap(
                "D",
                rate,
                None,
                None,
                False,
                window_ms,
                computed=True,
                note="sink volume unavailable — D not projected",
            )
        )
    return taps


def run_diagnostic(
    *,
    anchor,
    slot,
    signal,
    backend,
    spawn=_default_spawn,
    read_measfd=_default_read_measfd,
    port_allocator=None,
    rate=16000,
):
    """Run a diagnostic measurement for the given anchor point.

    This synchronous entry point supports **anchor C only**.

    * **C** — calibrated inject via ``build_measured_inject_argv``.  A sine wave at
      the requested level is injected into the TX PipeWire node and the raw PCM is
      captured from the fdsink tap.  D is computed from C + the static sink volume
      gain (``collect_static_gains``).

    Anchor **U** (server-originated headless reference) is handled by the engine's
    non-blocking path: ``_start_u_diagnostic`` installs a :class:`MeasuredTxBridge`,
    feeds incoming ``DIAG_STREAM_REF`` Opus frames through it, and then
    ``finish_u_diagnostic`` reads the tap and assembles the report.  ``run_diagnostic``
    is never called for anchor U in normal operation; the guard below is
    defense-in-depth for direct callers.

    **D** is always computed from C + measured sink volume on real HW.  The
    ``build_reverse_tap_argv`` helper exists as a foundation for a possible future
    direct D measurement (on-station or in a sim loopback), but ``run_diagnostic``
    does not use it today.
    """
    if anchor == "U":
        # Defense-in-depth: anchor U is handled by the engine's non-blocking
        # follow-up path (finish_u_diagnostic); run_diagnostic only supports C.
        return {
            "anchor": "U",
            "error": (
                "anchor U is handled by the engine's non-blocking follow-up path "
                "(finish_u_diagnostic); run_diagnostic supports anchor C only"
            ),
        }
    freq = int(signal.get("freq_hz", REF_FREQ_HZ))
    level = float(signal.get("level_dbfs", REF_LEVEL_DBFS))
    # Clamp duration on BOTH sides — negative/zero would produce invalid nbytes;
    # floor at REF_SETTLE_MS + REF_WINDOW_MS so there is always settle lead-in + a
    # full capture window (a floor of only REF_WINDOW_MS would leave no room for the
    # 200 ms settle, so the measurement window would capture startup transients).
    dur_ms = max(
        REF_SETTLE_MS + REF_WINDOW_MS,
        min(int(signal.get("duration_ms", REF_SETTLE_MS + REF_WINDOW_MS)), MAX_DURATION_MS),
    )
    tx_node = backend.resolve_node(slot, "tx")
    if tx_node is None:
        return {"anchor": anchor, "error": f"no TX node for slot {slot}"}
    gains = collect_static_gains(backend, slot)

    if anchor == "C":
        # BUG1: build make_argv as a closure so _default_spawn can bake the real write-fd
        # into the argv rather than using the hardcoded MEAS_FD constant.
        make_argv = lambda mfd: build_measured_inject_argv(  # noqa: E731
            tx_node, freq, level, rate, meas_fd=mfd
        )
    else:
        return {"anchor": anchor, "error": f"unknown anchor {anchor!r}"}

    nbytes = int(rate * dur_ms / 1000) * 2
    proc, read_fd = spawn(make_argv)
    try:
        pcm = read_measfd(read_fd, nbytes, dur_ms / 1000 + 1.0)
    finally:
        _terminate_proc(proc)

    # Use the last window_ms of the captured PCM (drop settle lead-in).
    win_bytes = int(rate * REF_WINDOW_MS / 1000) * 2
    window = pcm[-win_bytes:] if len(pcm) > win_bytes else pcm
    c_rms, c_peak, c_silent = rms_peak_dbfs(window)
    taps = build_cd_taps(c_rms, c_peak, c_silent, gains, rate, REF_WINDOW_MS)

    return {
        "anchor": anchor,
        "reference": {"freq_hz": freq, "level_dbfs": level, "window_ms": REF_WINDOW_MS},
        "taps": taps,
        "static_gains": gains,
    }


def finish_u_diagnostic(bridge, *, backend, slot: int, signal: dict, rate: int) -> dict:
    """Read the U diagnostic bridge's PCM tap and assemble the run report.

    Reads SETTLE+WINDOW ms of PCM, measures the trailing WINDOW ms at C, projects D
    from the static sink volume. Blocking (fd read) — call off the event loop.

    Anchor U measures the committed reference fixture (1 kHz / −20 dBFS); the
    client-supplied ``signal`` freq/level/duration are NOT honored for U (unlike
    anchor C) because U replays a fixed fixture injected by the server orchestrator.
    The ``signal`` parameter remains in the signature for API compatibility but does
    not drive the reported ``reference`` field.
    """
    nbytes = int(rate * (REF_SETTLE_MS + REF_WINDOW_MS) / 1000) * 2
    # Generous timeout: gst spawn + jitterbuffer latency before PCM flows.
    pcm = bridge.read_measurement(nbytes, (REF_SETTLE_MS + REF_WINDOW_MS) / 1000 + 3.0)
    win_bytes = int(rate * REF_WINDOW_MS / 1000) * 2
    window = pcm[-win_bytes:] if len(pcm) > win_bytes else pcm
    c_rms, c_peak, c_silent = rms_peak_dbfs(window)
    gains = collect_static_gains(backend, slot)
    taps = build_cd_taps(c_rms, c_peak, c_silent, gains, rate, REF_WINDOW_MS)
    return {
        "anchor": "U",
        "reference": {
            "freq_hz": REF_FREQ_HZ,
            "level_dbfs": REF_LEVEL_DBFS,
            "window_ms": REF_WINDOW_MS,
        },
        "taps": taps,
        "static_gains": gains,
    }


def _terminate_proc(proc) -> None:
    try:
        if hasattr(proc, "poll") and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                proc.kill()
    except Exception as exc:  # noqa: BLE001
        _log.debug("diag: terminate failed: %s", exc)


def _default_udp_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


class MeasuredTxBridge:
    """Anchor-U diagnostic TX bridge: receives op.mic Opus frames over UDP loopback,
    injects them into *tx_node* (output_MONO = C), and taps the post-decode PCM via an
    fdsink measurement pipe. Non-blocking — ``feed_opus`` is called from the WS loop as
    reference frames arrive; the captured PCM is read separately via ``read_measurement``.

    RF SAFETY: writes only to the PipeWire sink; never keys the SA818.
    """

    def __init__(
        self,
        tx_node: str,
        port: int,
        rate: int,
        *,
        spawn=_default_spawn,
        read_measfd=_default_read_measfd,
        socket_factory=None,
        ssrc: int = 0x5852_5841,
        dsp=None,
        dsp_probe=None,
    ):
        self._dsp = dsp
        self._dsp_probe = dsp_probe or probe_dsp_available
        self._node = tx_node
        self._port = port
        self._rate = rate
        self._spawn = spawn
        self._read_measfd = read_measfd
        self._socket_factory = socket_factory or _default_udp_socket
        self._ssrc = ssrc
        self._proc = None
        self._sock = None
        self._read_fd = None
        self._fd_lock = threading.Lock()
        self._seq = 0
        self._ts = 0

    def start(self) -> None:
        self._sock = self._socket_factory()
        dsp = self._dsp
        if dsp is not None and dsp.enabled and not self._dsp_probe():
            # Probe runs here (worker thread via the engine's _to_thread), never on the WS
            # loop. DSP elements missing -> measure the plain path, like TxBridge degrades.
            dsp = self._dsp = dataclasses.replace(dsp, enabled=False)
        # A disabled config means "no DSP in path": measure without the chain.
        dsp = dsp if (dsp is not None and dsp.enabled) else None
        make_argv = lambda mfd: build_measured_tx_argv(  # noqa: E731
            self._node, self._port, self._rate, meas_fd=mfd, dsp=dsp
        )
        self._proc, self._read_fd = self._spawn(make_argv)

    def feed_opus(self, payload: bytes) -> None:
        if self._sock is None:
            _log.debug("diag-bridge: feed before start; dropping")
            return
        datagram = _rtp.wrap_rtp(payload, seq=self._seq, ts=self._ts, ssrc=self._ssrc, pt=_RTP_PT)
        try:
            self._sock.sendto(datagram, (_LOOPBACK, self._port))
        except OSError as exc:
            _log.debug("diag-bridge: sendto failed: %s", exc)
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + RTP_TS_PER_FRAME) & 0xFFFFFFFF  # RTP 48 kHz clock, 20 ms frame

    def read_measurement(self, nbytes: int, timeout: float) -> bytes:
        # Atomically take ownership of the fd under the lock so stop() cannot
        # close it from another thread while we are mid-read.
        with self._fd_lock:
            fd = self._read_fd
            self._read_fd = None
        if fd is None:
            return b""
        # Blocking read happens OUTSIDE the lock so stop() is not held up.
        # _read_measfd owns closing fd (its documented contract). We MUST NOT
        # close it ourselves: a `finally: os.close(fd)` would double-close, and
        # under concurrency another thread could open()/reuse that fd number in
        # between, so the second close would hit the WRONG descriptor — the exact
        # race this ownership handoff exists to prevent.
        return self._read_measfd(fd, nbytes, timeout)

    def stop(self) -> None:
        # Atomically take ownership of the fd (if any) so read_measurement
        # cannot race on it after we decide to close.
        with self._fd_lock:
            fd = self._read_fd
            self._read_fd = None
        # Always terminate the gst process first — this closes the pipe
        # write-end, which unblocks any thread already mid-read on the read-end.
        _terminate_proc(self._proc)
        self._proc = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if fd is not None:
            try:
                _os.close(fd)
            except OSError:
                pass
