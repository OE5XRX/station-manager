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
    tx_node: str, port: int, rate: int, *, meas_fd: int = MEAS_FD
) -> list[str]:
    """Build a gst-launch argv that taps a UDP RTP stream into *tx_node* and measures PCM.

    ``meas_fd`` is the pipe write-end fd the child will write to; see
    :func:`build_measured_inject_argv` for the rationale.
    """
    caps = f"application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS,payload={_RTP_PT}"
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

    Supported anchors:

    * **C** — calibrated inject via ``build_measured_inject_argv``.  A sine wave at
      the requested level is injected into the TX PipeWire node and the raw PCM is
      captured from the fdsink tap.  D is computed from C + the static sink volume
      gain (``collect_static_gains``).

    Anchor **U** (server-originated headless reference) is reserved for a future
    on-station follow-up.  The engine never installs a feeding bridge for U and
    ``ws_client`` awaits commands serially so reference frames cannot arrive; the
    anchor is therefore not functional yet.  ``run_diagnostic`` fails fast with an
    error dict rather than binding a UDP port and measuring silence.

    **D** is always computed from C + measured sink volume on real HW.  The
    ``build_reverse_tap_argv`` helper exists as a foundation for a possible future
    direct D measurement (on-station or in a sim loopback), but ``run_diagnostic``
    does not use it today.
    """
    if anchor == "U":
        return {
            "anchor": "U",
            "error": (
                "anchor U (server-originated headless reference) is an on-station "
                "follow-up and not yet functional; use anchor C"
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
