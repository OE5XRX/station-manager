"""``python -m station_agent selftest audio`` — on-target audio-path self-check (Spec 0 §7/§8).

Uses Session A's sim substrate directly (the ``snd-aloop`` card with the 1 kHz tone shim,
tagged ``OE5XRX_SLOT``). Two checks, both asserted with the pure-Python Goertzel probe:

1. **RX:** tap ``oe5xrx.slot<N>`` through a full Opus encode→decode roundtrip and confirm the
   1 kHz shim tone survives (exercises the real 8 k→48 k→8 k resample + Opus path).
2. **TX:** play a *distinct* tone (default 1500 Hz) into ``oe5xrx.slot<N>.tx``; the sim's
   reverse cable (§8) re-exposes it on the capture, where we confirm the 1500 Hz peak. The
   distinct frequency keeps RX and TX from being confused.

This needs the audio-capable image (PipeWire + GStreamer). Per the audio-boundary honesty
rule (analog to the serial rule in station-manager/CLAUDE.md) it is only truly green on real
CM4/bench HW — sim-green is necessary, not sufficient. The pipeline builders + verdict logic
are pure and unit-tested; the subprocess capture/play seams are injected so CI needs no gst.
"""

from __future__ import annotations

import logging
import struct
import subprocess
import sys

from station_agent.audio.goertzel import dominant_bin, goertzel_power
from station_agent.audio.router_backend import PipeWireRouterBackend

logger = logging.getLogger("station_agent.audio.selftest")

RX_TONE_HZ = 1000  # the sim shim tone
_LOUD_MARGIN = 4.0  # dominant bin must beat the runner-up comfortably (guards against noise)


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
        "audio-type=voip",
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
    """Generate a ``freq`` sine → Opus roundtrip → inject into ``tx_node``."""
    return [
        "gst-launch-1.0",
        "-q",
        "audiotestsrc",
        "wave=sine",
        f"freq={freq}",
        "!",
        f"audio/x-raw,rate={rate},channels=1",
        "!",
        "audioconvert",
        "!",
        "opusenc",
        "audio-type=voip",
        "frame-size=20",
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


def _dominates(pcm: bytes, target: int, others: list[int], rate: int) -> bool:
    samples = _unpack_s16le(pcm)
    if not samples:
        return False
    p_target = goertzel_power(samples, target, rate)
    p_other = max((goertzel_power(samples, f, rate) for f in others), default=0.0)
    return p_target > _LOUD_MARGIN * max(p_other, 1e-12)


def _capture(argv: list[str], duration: float) -> bytes:
    try:
        proc = subprocess.run(  # noqa: S603 — fixed tool + resolved node
            argv, capture_output=True, timeout=duration + 5.0
        )
        return proc.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("selftest audio: capture failed: %s", exc)
        return b""


def _play(argv: list[str], duration: float) -> None:
    try:
        subprocess.run(argv, capture_output=True, timeout=duration + 5.0)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("selftest audio: play failed: %s", exc)


def run_audio(
    *,
    slot: int,
    tx_freq: int = 1500,
    rate: int = 8000,
    duration: float = 1.0,
    backend=None,
    capture=_capture,
    play=_play,
    capture_tx=None,
) -> int:
    """Run the RX + TX self-checks for ``slot``. Returns 0 on success, 1 on any failure."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    backend = backend or PipeWireRouterBackend()
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
    if not _dominates(rx_pcm, RX_TONE_HZ, [500, 1500, 2000, 3000], rate):
        logger.error("selftest audio: FAIL — RX %d Hz not dominant on %s", RX_TONE_HZ, rx_node)
        return 1
    logger.info("selftest audio: RX OK — %d Hz recovered through Opus", RX_TONE_HZ)

    # --- TX: distinct tone injected into the sink appears on the reverse capture ---
    logger.info("selftest audio: TX playing %d Hz into %s", tx_freq, tx_node)
    play(build_tx_play_argv(tx_node, tx_freq, rate), duration)
    tx_pcm = capture_tx(build_rx_check_argv(rx_node, rate), duration)
    if not _dominates(tx_pcm, tx_freq, [RX_TONE_HZ, 500, 2000, 3000], rate):
        logger.error("selftest audio: FAIL — TX %d Hz not detected on reverse capture", tx_freq)
        return 1
    logger.info("selftest audio: TX OK — %d Hz detected after injection", tx_freq)

    logger.info("selftest audio: PASS (slot %s) — only truly green on real CM4/bench HW", slot)
    return 0
