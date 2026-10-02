"""Server-side TX audio-path diagnostic report assembly.

Takes the agent's run_diagnostic output (anchor, reference, taps, static_gains)
and adds per-stage deltas and a human verdict.  Pure: no I/O, no Django models.

Also provides the U-anchor Opus reference fixture loader and §5.3 media-frame
iterator used by the server-side diagnostic injection path.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path

from station_agent.audio.frame import pack_frame

# ---------------------------------------------------------------------------
# U-anchor Opus reference fixture
# ---------------------------------------------------------------------------

#: Default path to the committed .opusframes fixture — a sequence of
#: length-prefixed raw Opus packets encoding a 1 kHz −20 dBFS sine at 16 kHz.
DEFAULT_REF_PATH: Path = Path(__file__).parent / "data" / "ref_1khz_-20dbfs_16k.opusframes"

# Samples per 20 ms frame at 16 kHz.
_SAMPLES_PER_FRAME: int = 16000 * 20 // 1000  # 320


def load_reference_frames(path: Path = DEFAULT_REF_PATH) -> list[bytes]:
    """Read a length-prefixed ``.opusframes`` file into a list of Opus payloads.

    Each record in the file is a big-endian ``uint16`` length followed by that
    many bytes of raw Opus data.  A trailing partial record (truncated file) is
    silently skipped so the function remains safe against partial writes.

    Parameters
    ----------
    path:
        Path to the ``.opusframes`` file.  Defaults to :data:`DEFAULT_REF_PATH`.

    Returns
    -------
    list[bytes]
        One entry per packet; each entry is a non-empty :class:`bytes` object.
    """
    data = path.read_bytes()
    frames: list[bytes] = []
    offset = 0
    while offset < len(data):
        # Need at least 2 bytes for the length prefix.
        if offset + 2 > len(data):
            break  # trailing partial record — stop
        (length,) = struct.unpack_from(">H", data, offset)
        offset += 2
        if offset + length > len(data):
            break  # trailing partial payload — stop
        frames.append(data[offset : offset + length])
        offset += length
    return frames


def iter_media_frames(
    frames: list[bytes],
    stream_ref: int,
    *,
    repeat: int,
    seq0: int = 0,
) -> Iterator[bytes]:
    """Wrap Opus packets into §5.3 media frames for TX injection.

    Each packet in *frames* is packed into a media frame via
    :func:`station_agent.audio.frame.pack_frame`.  The list is looped
    *repeat* times so the caller can cover any diagnostics window without
    storing more data than needed.  ``seq`` advances monotonically across the
    whole loop; ``ts`` advances by :data:`_SAMPLES_PER_FRAME` (320) per frame.

    Parameters
    ----------
    frames:
        List of raw Opus payloads (e.g. from :func:`load_reference_frames`).
    stream_ref:
        The numeric stream handle to embed in each §5.3 frame header.
    repeat:
        How many times to loop *frames*.
    seq0:
        Starting sequence number (default 0).

    Yields
    ------
    bytes
        One packed §5.3 media frame per iteration.
    """
    seq = seq0
    ts = 0
    for _ in range(repeat):
        for payload in frames:
            yield pack_frame(
                stream_ref=stream_ref,
                seq=seq,
                ts=ts,
                flags=0,
                payload=payload,
            )
            seq += 1
            ts += _SAMPLES_PER_FRAME


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

# 20·log10(0.40) ≈ -7.96 dB — the expected sink-volume stage attenuation.
SINK_EXPECTED_DB: float = -7.96


def build_run_report(agent_report: dict) -> dict:
    """Augment *agent_report* with ``stages`` and ``verdict``.

    Parameters
    ----------
    agent_report:
        Dict produced by ``station_agent.audio.diagnostics.run_diagnostic``:
        ``{anchor, reference, taps, static_gains}``.

    Returns
    -------
    dict
        Same keys as *agent_report* plus ``stages`` (list) and ``verdict`` (str).
    """
    taps = agent_report["taps"]
    static_gains = agent_report["static_gains"]
    # May be None when the agent could not read the sink volume (wpctl failed).
    sink_db: float | None = static_gains.get("sink_volume_db")

    # Build a point→tap lookup for verdict logic.
    tap_by_point: dict[str, dict] = {t["point"]: t for t in taps}

    # --- stages: one entry per adjacent pair ---------------------------------
    stages: list[dict] = []
    for i in range(len(taps) - 1):
        src = taps[i]
        dst = taps[i + 1]
        from_pt: str = src["point"]
        to_pt: str = dst["point"]

        src_rms = src.get("rms_dbfs")
        dst_rms = dst.get("rms_dbfs")

        delta_db: float | None = None
        if src_rms is not None and dst_rms is not None:
            delta_db = round(dst_rms - src_rms, 2)

        stage: dict = {"from": from_pt, "to": to_pt, "delta_db": delta_db, "note": None}

        # Attach expected_db for the known sink stage (C→D). Keep the key for
        # schema stability even when the sink volume could not be read (None).
        if from_pt == "C" and to_pt == "D":
            stage["expected_db"] = sink_db if sink_db is not None else None

        stages.append(stage)

    # --- verdict: first-match wins -------------------------------------------
    c_tap = tap_by_point.get("C")
    verdict: str

    if c_tap is None or c_tap.get("silent") or c_tap.get("rms_dbfs") is None:
        # Rule 1: C is silent → inject/agent path broken.
        verdict = "no signal at C — inject/agent path broken"
    elif (c_tap.get("peak_dbfs") is not None) and c_tap["peak_dbfs"] < -12.0:
        # Rule 3 (brief priority): C peak is low → upstream loss.
        verdict = "low level already at C — loss upstream (agent/opus or injected reference)"
    else:
        # Check for clean digital chain: C→D delta ≈ sink_volume_db and C near full-scale.
        cd_stage = next((s for s in stages if s["from"] == "C" and s["to"] == "D"), None)
        c_peak = c_tap.get("peak_dbfs")
        if (
            cd_stage is not None
            and cd_stage["delta_db"] is not None
            and sink_db is not None
            and abs(cd_stage["delta_db"] - sink_db) <= 1.0
            and c_peak is not None
            and c_peak > -6.0
        ):
            verdict = (
                f"digital chain clean to D; C->D loss is the sink volume stage"
                f" ({sink_db:.2f} dB) — expected"
            )
        else:
            verdict = "audio path check completed — review stage deltas for anomalies"

    result = dict(agent_report)
    result["stages"] = stages
    result["verdict"] = verdict
    return result
