"""Server-side TX audio-path diagnostic report assembly.

Takes the agent's run_diagnostic output (anchor, reference, taps, static_gains)
and adds per-stage deltas and a human verdict.  Pure: no I/O, no Django models.
"""

from __future__ import annotations

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
    sink_volume_db: float = static_gains["sink_volume_db"]

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

        # Attach expected_db for the known sink stage (C→D).
        if from_pt == "C" and to_pt == "D":
            stage["expected_db"] = sink_volume_db

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
            and abs(cd_stage["delta_db"] - sink_volume_db) <= 1.0
            and c_peak is not None
            and c_peak > -6.0
        ):
            verdict = (
                f"digital chain clean to D; C->D loss is the sink volume stage"
                f" ({sink_volume_db:.2f} dB) — expected"
            )
        else:
            verdict = "audio path check completed — review stage deltas for anomalies"

    result = dict(agent_report)
    result["stages"] = stages
    result["verdict"] = verdict
    return result
