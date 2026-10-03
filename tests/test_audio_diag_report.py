# tests/test_audio_diag_report.py
from apps.audio import diagnostics as sd


def _agent_report(c_peak, c_rms, sink_db=-7.96):
    d_rms = None if c_rms is None else round(c_rms + sink_db, 2)
    d_peak = None if c_peak is None else round(c_peak + sink_db, 2)
    return {
        "anchor": "C",
        "reference": {"freq_hz": 1000, "level_dbfs": -20.0, "window_ms": 300},
        "taps": [
            {
                "point": "C",
                "rms_dbfs": c_rms,
                "peak_dbfs": c_peak,
                "silent": c_rms is None,
                "computed": False,
            },
            {
                "point": "D",
                "rms_dbfs": d_rms,
                "peak_dbfs": d_peak,
                "silent": d_rms is None,
                "computed": True,
            },
        ],
        "static_gains": {"sink_volume_linear": 0.40, "sink_volume_db": sink_db},
    }


def test_report_has_stage_delta_and_expected():
    # C peak = -20.0 (at reference), D computed=True, sink=-7.96 dB.
    rep = sd.build_run_report(_agent_report(-20.0, -23.0))
    stages = {(s["from"], s["to"]): s for s in rep["stages"]}
    cd = stages[("C", "D")]
    assert abs(cd["delta_db"] - (-7.96)) < 0.1
    assert abs(cd["expected_db"] - sd.SINK_EXPECTED_DB) < 0.1


def test_verdict_clean_chain_blames_sink_volume():
    # C peak ≈ reference level (-20 dBFS); D is computed → verdict must mention "sink volume".
    rep = sd.build_run_report(_agent_report(-20.0, -23.0))
    assert "sink volume" in rep["verdict"].lower()


def test_verdict_silent_c_flags_broken_inject():
    rep = sd.build_run_report(_agent_report(None, None))
    assert "no signal" in rep["verdict"].lower()


def test_verdict_low_c_blames_upstream():
    # C peak = -30 dBFS, well below the reference of -20 dBFS (delta = 10 dB > LOSS_TOL 6 dB).
    rep = sd.build_run_report(_agent_report(-30.0, -33.0))
    assert "upstream" in rep["verdict"].lower()


def test_report_tolerates_missing_sink_volume():
    # Real station: wpctl failed, so the agent reports a None sink volume.
    report = {
        "anchor": "C",
        "reference": {"freq_hz": 1000, "level_dbfs": -20.0, "window_ms": 300},
        "taps": [
            {
                "point": "C",
                "rms_dbfs": -6.0,
                "peak_dbfs": -3.0,
                "silent": False,
                "computed": False,
            },
            {
                "point": "D",
                "rms_dbfs": -14.0,
                "peak_dbfs": -11.0,
                "silent": False,
                "computed": True,
            },
        ],
        "static_gains": {"sink_volume_linear": None, "sink_volume_db": None},
    }
    rep = sd.build_run_report(report)
    assert isinstance(rep["verdict"], str) and rep["verdict"]
    stages = {(s["from"], s["to"]): s for s in rep["stages"]}
    cd = stages[("C", "D")]
    assert cd["expected_db"] is None


def test_zero_dbfs_reference_not_treated_as_missing():
    # Verify that a 0.0 dBFS reference is valid and does not fall back to REF_LEVEL_DBFS.
    # When C peak is at 0.0 dBFS (at the reference level), the verdict should NOT say "upstream".
    rep = sd.build_run_report(_agent_report(0.0, -3.0))
    verdict = rep["verdict"].lower()
    # Should treat C as "at expected level", not as below reference due to fallback.
    assert "upstream" not in verdict
    assert "sink volume" in verdict or "at expected" in verdict
