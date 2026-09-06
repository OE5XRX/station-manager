"""``python -m station_agent selftest audio`` — on-target audio-path self-check (Spec 0 §7/§8).

Uses Session A's sim substrate directly (the ``snd-aloop`` card with the 1 kHz tone shim,
tagged ``OE5XRX_SLOT``). Two checks, both asserted with the pure-Python Goertzel probe:

1. **RX:** tap ``oe5xrx.slot<N>`` through a full Opus encode→decode roundtrip and confirm the
   1 kHz shim tone survives (exercises the real 8 k→48 k→8 k resample + Opus path).
2. **TX:** play a *distinct* tone (default 1500 Hz) through an Opus roundtrip into
   ``oe5xrx.slot<N>.tx`` and confirm it on the sim's **reverse-cable tap** — the raw
   ``snd-aloop`` **dev0 capture** (Spec 0 §8), the SAME end A's reference
   ``tests/audio/audio_selfcheck.sh`` records. The tone plays in the **background** while the
   tap records (a real-time loopback carries nothing unless the source is live during capture).
   The distinct frequency keeps RX (1 kHz) from being confused with TX (1500 Hz).

This needs the audio-capable image (PipeWire + GStreamer + alsa-utils). Per the audio-boundary
honesty rule (analog to the serial rule in station-manager/CLAUDE.md), **sim-green is necessary,
not sufficient — the RX path must still be confirmed on real CM4/bench HW.** The RX check is
substrate-agnostic (it works identically in sim and on real HW). **The TX check, by contrast, is
a sim-loopback capability:** the reverse cable exists only in the ``snd-aloop`` substrate; on the
real UAC2 FM module the TX playback EP does not loop back to the RX capture EP (TX leaves as RF),
so ``run_audio`` **fails closed** there (no reverse tap → FAIL) and real-HW TX verification is a
separate RF/bench concern (documented follow-up), not this electrical self-check.

The pipeline builders + verdict logic are pure and unit-tested; the subprocess capture/play/spawn
seams are injected so CI needs no GStreamer.
"""

from __future__ import annotations

import logging
import struct
import subprocess
import sys
import time

from station_agent.audio.goertzel import dominant_bin, goertzel_power

logger = logging.getLogger("station_agent.audio.selftest")

RX_TONE_HZ = 1000  # the sim shim tone
_LOUD_MARGIN = 4.0  # dominant bin must beat the runner-up comfortably (guards against noise)
_TX_RUNNING_TIMEOUT = 10.0  # bound the wait for the aloop TX playback to reach RUNNING under TCG


def build_rx_check_argv(rx_node: str, rate: int) -> list[str]:
    """Tap ``rx_node`` → Opus encode → Opus decode → raw S16LE PCM on stdout."""
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
        f"audio/x-raw,format=S16LE,rate={rate},channels=1",
        "!",
        "fdsink",
        "fd=1",
    ]


def build_tx_play_argv(tx_node: str, freq: int, rate: int) -> list[str]:
    """Generate a *live* ``freq`` sine → full Opus roundtrip → inject into ``tx_node``.

    Mirrors the production mic path (browser ``opusenc`` → agent ``opusdec`` → TX sink): the
    tone is Opus-encoded **and decoded** back to PCM before ``pipewiresink`` (raw PCM is what a
    sink consumes — feeding it ``opusenc`` output directly is a caps mismatch that never links).
    ``is-live=true`` keeps the tone flowing so the reverse-cable tap has a live signal to record.
    """
    return [
        "gst-launch-1.0",
        "-q",
        "audiotestsrc",
        "is-live=true",
        "wave=sine",
        f"freq={freq}",
        "!",
        f"audio/x-raw,rate={rate},channels=1",
        "!",
        "audioconvert",
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
        "pipewiresink",
        f"target-object={tx_node}",
        "sync=false",
    ]


def build_tx_capture_argv(tap: str, rate: int, duration: float) -> list[str]:
    """Raw-capture the sim reverse-cable tap (aloop **dev0 capture**) as S16LE PCM on stdout.

    Uses ``arecord`` on the raw ALSA hw device (``hw:<card>,0,0``) rather than a PipeWire node:
    WirePlumber intentionally disables the dev0 PipeWire nodes so the tone shim / TX self-check
    can own the raw ends without contention (Spec 0 §8 / 51-oe5xrx-slot-naming.conf). This is the
    exact tap A's ``audio_selfcheck.sh`` records. ``-d`` bounds the capture; ``_capture``'s own
    timeout is the backstop.
    """
    return [
        "arecord",
        "-q",
        "-t",
        "raw",
        "-f",
        "S16_LE",
        "-r",
        str(rate),
        "-c",
        "1",
        "-D",
        tap,
        "-d",
        str(max(1, int(round(duration)))),
        "-",
    ]


def _unpack_s16le(pcm: bytes) -> tuple[int, ...]:
    """Decode S16LE ``pcm`` to signed samples (empty tuple if there are none)."""
    n = len(pcm) // 2
    if n == 0:
        return ()
    return struct.unpack(f"<{n}h", pcm[: n * 2])


def analyze_pcm(pcm: bytes, candidates: list[int], rate: int) -> int | None:
    """Return the dominant candidate frequency in S16LE ``pcm``, or None if empty."""
    samples = _unpack_s16le(pcm)
    if not samples:
        return None
    return dominant_bin(samples, candidates, rate)


def dominance(pcm: bytes, target: int, others: list[int], rate: int) -> float:
    """Ratio of ``target``-bin power to the strongest of ``others`` (0.0 on empty PCM).

    Returned (not just thresholded) so the caller can log the actual FFT margin as evidence.
    """
    samples = _unpack_s16le(pcm)
    if not samples:
        return 0.0
    p_target = goertzel_power(samples, target, rate)
    p_other = max((goertzel_power(samples, f, rate) for f in others), default=0.0)
    return p_target / max(p_other, 1e-12)


def _capture(argv: list[str], duration: float) -> bytes:
    try:
        proc = subprocess.run(  # noqa: S603 — fixed tool + resolved node/hw device
            argv, capture_output=True, timeout=duration + 5.0
        )
        # A pipeline that error-exits (bad element/property/caps) returns fast with empty
        # stdout — surface its stderr so the failure is diagnosable instead of a silent
        # ratio=0.00 (this is how a bad opusenc property would otherwise hide).
        if proc.returncode != 0 or not proc.stdout:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            if err:
                logger.warning(
                    "selftest audio: capture pipeline rc=%s, stderr: %s",
                    proc.returncode,
                    err[-800:],
                )
        return proc.stdout
    except subprocess.TimeoutExpired as exc:
        # EXPECTED for the RX pipeline: `gst-launch pipewiresrc ! … ! fdsink` has no
        # num-buffers, so it streams until we kill it at the timeout. The captured PCM
        # lives on the exception (capture_output=True) — salvage it rather than discard
        # the whole recording as b"" (which would make RX/TX always fail with no signal).
        return exc.stdout or b""
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("selftest audio: capture failed: %s", exc)
        return b""


def _spawn(argv: list[str]):
    """Start a long-running pipeline in the background; caller stops it. Returns a Popen."""
    return subprocess.Popen(  # noqa: S603 — fixed tool + resolved node
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _stop(proc) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _await_tx_running(
    card: int, *, timeout: float = _TX_RUNNING_TIMEOUT, sleep=time.sleep
) -> None:
    """Wait until the aloop TX playback (dev1 playback) reaches RUNNING before tapping.

    A blind sleep races ``pipewiresink`` link-up under TCG; polling the ALSA pcm status is what
    A's reference does. Best-effort: if the status file never shows RUNNING we still proceed and
    let the Goertzel verdict fail loudly rather than hang.
    """
    path = f"/proc/asound/card{card}/pcm1p/sub0/status"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(path) as fh:
                if "RUNNING" in fh.read():
                    return
        except OSError:
            pass
        sleep(0.1)
    logger.warning(
        "selftest audio: TX playback did not reach RUNNING within %.0fs (%s)", timeout, path
    )


def _reverse_tap_for_slot(backend, slot: int) -> tuple[str, int] | None:
    """Resolve the sim reverse-cable tap ``(hw:<card>,0,0, card)`` for ``slot`` via the backend.

    The card index comes from the same ``OE5XRX_SLOT`` udev → ALSA card resolution the backend
    already uses for node lookup — slot-parametric, no hardcoded aloop id (sim=slot1, bench=slot3).
    """
    resolver = getattr(backend, "alsa_card_for_slot", None)
    if resolver is None:
        return None
    card = resolver(slot)
    if card is None:
        return None
    return f"hw:{card},0,0", card


def run_audio(
    *,
    slot: int,
    tx_freq: int = 1500,
    rate: int = 8000,
    duration: float = 1.0,
    backend=None,
    capture=_capture,
    capture_tx=None,
    spawn=_spawn,
    wait_running=None,
    tx_tap: str | None = None,
) -> int:
    """Run the RX + TX self-checks for ``slot``. Returns 0 on success, 1 on any failure."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if backend is None:
        from station_agent.audio.router_backend import PipeWireRouterBackend

        backend = PipeWireRouterBackend()
    capture_tx = capture_tx or capture

    rx_node = backend.resolve_node(slot, "rx")
    tx_node = backend.resolve_node(slot, "tx")
    if rx_node is None or tx_node is None:
        logger.error(
            "selftest audio: FAIL — could not resolve slot %s nodes (rx=%s tx=%s)",
            slot,
            rx_node,
            tx_node,
        )
        return 1

    # --- RX: 1 kHz shim tone survives the Opus roundtrip ---
    logger.info("selftest audio: RX tapping %s (expect %d Hz)", rx_node, RX_TONE_HZ)
    rx_pcm = capture(build_rx_check_argv(rx_node, rate), duration)
    rx_ratio = dominance(rx_pcm, RX_TONE_HZ, [500, 1500, 2000, 3000], rate)
    if rx_ratio <= _LOUD_MARGIN:
        logger.error(
            "selftest audio: FAIL — RX %d Hz not dominant on %s (ratio=%.2f, need >%.1f)",
            RX_TONE_HZ,
            rx_node,
            rx_ratio,
            _LOUD_MARGIN,
        )
        return 1
    logger.info(
        "selftest audio: RX OK — %d Hz recovered through Opus (P(%d)/P(runner-up)=%.1fx)",
        RX_TONE_HZ,
        RX_TONE_HZ,
        rx_ratio,
    )

    # --- TX: distinct tone injected into the sink appears on the reverse-cable tap ---
    card = None
    if tx_tap is None:
        resolved = _reverse_tap_for_slot(backend, slot)
        if resolved is not None:
            tx_tap, card = resolved
    if tx_tap is None:
        logger.error(
            "selftest audio: FAIL — no reverse-cable TX tap for slot %s "
            "(need the sim aloop dev0 capture; real-HW TX verification is RF/bench)",
            slot,
        )
        return 1

    if wait_running is None and card is not None:
        wait_running = lambda: _await_tx_running(card)  # noqa: E731 — tiny bound closure
    elif wait_running is None:
        wait_running = lambda: None  # noqa: E731

    logger.info(
        "selftest audio: TX playing %d Hz into %s, tapping reverse cable %s",
        tx_freq,
        tx_node,
        tx_tap,
    )
    try:
        proc = spawn(build_tx_play_argv(tx_node, tx_freq, rate))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("selftest audio: FAIL — could not start TX tone: %s", exc)
        return 1
    try:
        wait_running()
        tx_pcm = capture_tx(build_tx_capture_argv(tx_tap, rate, duration), duration)
    finally:
        _stop(proc)

    tx_ratio = dominance(tx_pcm, tx_freq, [RX_TONE_HZ, 500, 2000, 3000], rate)
    if tx_ratio <= _LOUD_MARGIN:
        logger.error(
            "selftest audio: FAIL — TX %d Hz not dominant on reverse tap %s "
            "(ratio=%.2f, need >%.1f)",
            tx_freq,
            tx_tap,
            tx_ratio,
            _LOUD_MARGIN,
        )
        return 1
    logger.info(
        "selftest audio: TX OK — %d Hz on reverse tap (P(%d)/P(runner-up)=%.1fx)",
        tx_freq,
        tx_freq,
        tx_ratio,
    )

    logger.info(
        "selftest audio: PASS (slot %s) — sim substrate; confirm RX on real CM4/bench HW "
        "(TX loopback is sim-only)",
        slot,
    )
    return 0
