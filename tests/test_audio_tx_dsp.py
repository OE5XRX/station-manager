import math
import shutil
import subprocess

import pytest

from station_agent.audio import tx_dsp
from station_agent.audio.opus_bridge import build_tx_argv


def _cfg(**k):
    return tx_dsp.TxDspConfig(ceiling_dbfs=k.pop("ceiling", -12.0), **k)


def _pipeline_order(argv):
    """Element names in pipeline order (tokens right after a '!' or the src)."""
    names = [argv[2]]
    for i, tok in enumerate(argv):
        if tok == "!" and i + 1 < len(argv):
            names.append(argv[i + 1].split(",")[0])
    return names


def test_plain_argv_unchanged_when_no_dsp():
    argv = build_tx_argv("n", 47000, 16000)
    assert "audiodynamic" not in argv and "tee" not in argv
    assert argv[-3:] == ["pipewiresink", "target-object=n", "sync=false"]


def test_full_chain_order_limiter_last_before_sink():
    argv = build_tx_argv("n", 47000, 16000, dsp=_cfg())
    order = [
        n
        for n in _pipeline_order(argv)
        if n in ("audiocheblimit", "audiodynamic", "volume", "pipewiresink")
    ]
    assert order == [
        "audiocheblimit",
        "audiocheblimit",
        "audiodynamic",
        "audiodynamic",
        "volume",
        "audiodynamic",
        "pipewiresink",
    ]
    # stage modes in order: expander (gate), compressor, then hard-knee limiter
    dyn = [i for i, t in enumerate(argv) if t == "audiodynamic"]
    assert "mode=expander" in argv[dyn[0] : dyn[0] + 5]
    assert "mode=compressor" in argv[dyn[1] : dyn[1] + 5]
    lim = argv[dyn[2] : dyn[2] + 5]
    assert "characteristics=hard-knee" in lim and "ratio=1000.0" in lim


def test_band_pass_corners():
    argv = build_tx_argv("n", 47000, 16000, dsp=_cfg())
    joined = " ".join(argv)
    assert "audiocheblimit mode=high-pass cutoff=300" in joined
    assert "audiocheblimit mode=low-pass cutoff=3000" in joined


def test_limiter_threshold_tracks_ceiling():
    argv = build_tx_argv("n", 47000, 16000, dsp=_cfg(ceiling=-6.0))
    want = f"threshold={tx_dsp.db_to_linear(-6.0):.6f}"
    dyn = [i for i, t in enumerate(argv) if t == "audiodynamic"]
    assert want in argv[dyn[2] : dyn[2] + 5]


def test_makeup_derived_from_ceiling():
    assert _cfg(ceiling=-12.0).makeup_db == -12.0 - 3.0 + 24.0
    assert _cfg(ceiling=-6.0).makeup_db == 15.0


def test_ceiling_is_reclamped_inside_config():
    assert _cfg(ceiling=10.0).ceiling_dbfs == -3.0
    assert _cfg(ceiling=float("nan")).ceiling_dbfs == -12.0


def test_dsp_runs_in_f32_and_converts_before_sink():
    argv = build_tx_argv("n", 47000, 16000, dsp=_cfg())
    assert "audio/x-raw,format=F32LE,rate=16000,channels=1" in argv
    sink = argv.index("pipewiresink")
    assert argv[sink - 2] == "audioconvert"


def test_degraded_is_passthrough_volume_and_no_limiter():
    argv = build_tx_argv("n", 47000, 16000, dsp=_cfg(enabled=False))
    assert "audiodynamic" not in argv and "audiocheblimit" not in argv
    assert "volume=1.0" in argv


def test_meter_tap_is_leaky_and_pre_limiter_f32_on_stdout():
    argv = build_tx_argv("n", 47000, 16000, dsp=_cfg(), meter=True)
    j = " ".join(argv)
    assert "tee name=txm" in j
    # the meter branch can never back-pressure TX audio
    assert "queue leaky=downstream max-size-buffers=8" in j
    assert j.rstrip().endswith("fdsink fd=1 sync=false")
    # tee sits before the limiter (pre-limiter tap)
    tee = argv.index("tee")
    dyn = [i for i, t in enumerate(argv) if t == "audiodynamic"]
    assert dyn[1] < tee < dyn[2]


def test_no_shell_quotes_in_any_token():
    for cfg in (_cfg(), _cfg(enabled=False)):
        for tok in build_tx_argv("n", 47000, 16000, dsp=cfg, meter=True):
            assert '"' not in tok and "'" not in tok


def test_db_to_linear():
    assert math.isclose(tx_dsp.db_to_linear(0.0), 1.0)
    assert math.isclose(tx_dsp.db_to_linear(-20.0), 0.1)


@pytest.mark.skipif(shutil.which("gst-launch-1.0") is None, reason="no gst")
def test_real_gst_accepts_dsp_fragment():
    for el in tx_dsp.DSP_ELEMENTS:
        if subprocess.run(["gst-inspect-1.0", "--exists", el]).returncode != 0:
            pytest.skip(f"{el} not installed")
    cfg = _cfg()
    argv = [
        "gst-launch-1.0",
        "-q",
        "audiotestsrc",
        "num-buffers=20",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        "audio/x-raw,format=F32LE,rate=16000,channels=1",
        *tx_dsp.pre_limiter_fragment(cfg),
        *tx_dsp.limiter_fragment(cfg),
        "!",
        "audioconvert",
        "!",
        "fakesink",
    ]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert "WARN" not in r.stderr.upper() and "WARNUNG" not in r.stderr.upper()
