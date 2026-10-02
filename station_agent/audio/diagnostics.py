"""Audio-path diagnostics: calibrated reference generation, dBFS metering, and
measured inject/tap pipelines for the TX chain (Spec: audio-path-diagnostics).

Pure DSP + argv builders are unit-tested; the subprocess capture/spawn seams are
injected (mirrors ``selftest.py``) so CI needs no GStreamer/PipeWire.

RF SAFETY: nothing here keys the SA818. Inject/measure end at the digital
ALSA/UAC2 edge (D); carrier keying is a separate control-plane action.
"""
from __future__ import annotations

import logging as _logging
import math
import os as _os
import re as _re
import struct
import subprocess as _subprocess

from station_agent.audio import selftest as _selftest

_log = _logging.getLogger(__name__)

FULL_SCALE_S16 = 32767

REF_FREQ_HZ = 1000
REF_LEVEL_DBFS = -20.0
REF_WINDOW_MS = 300
REF_SETTLE_MS = 200

MAX_DURATION_MS = 5000


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
    tx_node: str, freq_hz: int, level_dbfs: float, rate: int
) -> list[str]:
    linear = 10 ** (level_dbfs / 20)
    return [
        "gst-launch-1.0", "-q",
        "audiotestsrc", "is-live=true", "wave=sine", f"freq={freq_hz}",
        "!", f"audio/x-raw,rate={rate},channels=1",
        "!", "audioconvert",
        "!", "volume", f"volume={linear:g}",
        "!", "opusenc", "audio-type=voice", "frame-size=20", "inband-fec=true",
        "!", "opusdec", "plc=true",
        "!", "audioconvert", "!", "audioresample",
        "!", f"audio/x-raw,rate={rate},channels=1",
        "!", "tee", "name=t",
        "t.", "!", "queue", "!", "pipewiresink", f"target-object={tx_node}", "sync=false",
        "t.", "!", "queue", "!", "audioconvert",
        "!", f"audio/x-raw,format=S16LE,rate={rate},channels=1",
        "!", "fdsink", f"fd={MEAS_FD}",
    ]


def build_measured_tx_argv(tx_node: str, port: int, rate: int) -> list[str]:
    caps = f"application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload={_RTP_PT}"
    return [
        "gst-launch-1.0", "-q",
        "udpsrc", f"address={_LOOPBACK}", f"port={port}", f"caps={caps}",
        "!", "rtpjitterbuffer", "!", "rtpopusdepay",
        "!", "opusdec", "plc=true", "use-inband-fec=true",
        "!", "audioconvert", "!", "audioresample",
        "!", f"audio/x-raw,rate={rate},channels=1",
        "!", "tee", "name=t",
        "t.", "!", "queue", "!", "pipewiresink", f"target-object={tx_node}", "sync=false",
        "t.", "!", "queue", "!", "audioconvert",
        "!", f"audio/x-raw,format=S16LE,rate={rate},channels=1",
        "!", "fdsink", f"fd={MEAS_FD}",
    ]


def build_reverse_tap_argv(tap: str, rate: int, duration: float) -> list[str]:
    return _selftest.build_tx_capture_argv(tap, rate, duration)


_WPCTL_VOL = _re.compile(r"Volume:\s*([0-9]+\.[0-9]+)")


def parse_wpctl_volume(text: str) -> float | None:
    m = _WPCTL_VOL.search(text or "")
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


def _default_spawn(argv: list[str]):
    r, w = _os.pipe()                      # measurement fd handed to the child as fd 3
    proc = _subprocess.Popen(              # noqa: S603 — fixed tool + resolved node
        argv, stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
        pass_fds=(w,),
    )
    _os.close(w)
    proc._meas_read_fd = r                 # type: ignore[attr-defined]
    return proc


def _default_read_measfd(proc, nbytes: int, timeout: float) -> bytes:
    import select
    fd = getattr(proc, "_meas_read_fd")
    buf = bytearray()
    import time as _t
    deadline = _t.monotonic() + timeout
    while len(buf) < nbytes and _t.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], max(0.0, deadline - _t.monotonic()))
        if not r:
            break
        chunk = _os.read(fd, nbytes - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    try:
        _os.close(fd)
    except OSError:
        pass
    return bytes(buf)


def _tap(point, rate, rms, peak, silent, window_ms, computed):
    return {"point": point, "format": {"rate": rate, "channels": 1},
            "rms_dbfs": rms, "peak_dbfs": peak, "window_ms": window_ms,
            "silent": silent, "computed": computed}


def run_diagnostic(*, anchor, slot, signal, backend,
                   spawn=_default_spawn, read_measfd=_default_read_measfd,
                   port_allocator=None, reverse_tap=None, rate=16000):
    freq = int(signal.get("freq_hz", REF_FREQ_HZ))
    level = float(signal.get("level_dbfs", REF_LEVEL_DBFS))
    dur_ms = min(int(signal.get("duration_ms", REF_SETTLE_MS + REF_WINDOW_MS)), MAX_DURATION_MS)
    tx_node = backend.resolve_node(slot, "tx")
    if tx_node is None:
        return {"anchor": anchor, "error": f"no TX node for slot {slot}"}
    gains = collect_static_gains(backend, slot)

    if anchor == "C":
        argv = build_measured_inject_argv(tx_node, freq, level, rate)
    elif anchor == "U":
        port = (port_allocator.acquire() if port_allocator else 47000)
        argv = build_measured_tx_argv(tx_node, port, rate)
    else:
        return {"anchor": anchor, "error": f"unknown anchor {anchor!r}"}

    nbytes = int(rate * (dur_ms) / 1000) * 2
    proc = spawn(argv)
    try:
        pcm = read_measfd(proc, nbytes, dur_ms / 1000 + 1.0)
    finally:
        _terminate_proc(proc)
        if anchor == "U" and port_allocator:
            port_allocator.release(port)

    # Use the last window_ms of the captured PCM (drop settle lead-in).
    win_bytes = int(rate * REF_WINDOW_MS / 1000) * 2
    window = pcm[-win_bytes:] if len(pcm) > win_bytes else pcm
    c_rms, c_peak, c_silent = rms_peak_dbfs(window)
    taps = [_tap("C", rate, c_rms, c_peak, c_silent, REF_WINDOW_MS, computed=False)]

    sink_db = gains.get("sink_volume_db")
    if not c_silent and sink_db is not None:
        taps.append(_tap("D", rate, round(c_rms + sink_db, 2), round(c_peak + sink_db, 2),
                         False, REF_WINDOW_MS, computed=True))
    else:
        taps.append(_tap("D", rate, None, None, True, REF_WINDOW_MS, computed=True))

    return {"anchor": anchor,
            "reference": {"freq_hz": freq, "level_dbfs": level, "window_ms": REF_WINDOW_MS},
            "taps": taps, "static_gains": gains}


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
