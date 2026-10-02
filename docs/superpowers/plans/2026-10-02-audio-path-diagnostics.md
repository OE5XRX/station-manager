# Audio Path Diagnostics (TX chain) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable, AI-drivable measure-and-inject harness that reports dBFS levels at defined points along the TX audio chain (browser T0/T1, agent C/D) and injects calibrated reference signals at three anchors (U/T1/C), so digital level loss can be localized by signal-chain bisection without a human or a radio.

**Architecture:** The agent gains a diagnostics module that generates a calibrated reference, injects it into the existing gst TX pipeline (with an in-pipeline `tee`→measurement-fd tap so the injected signal is measured synchronously, independent of PipeWire monitor-port topology), and reports dBFS. The station-manager orchestrates a "diagnostic run" by pushing a `diag_command` down the already-open agent audio WebSocket and correlating the `diag_result` back to a REST caller via a per-request reply channel. The server stays a dumb relay; for the headless **U** anchor it replays a pre-encoded Opus reference fixture into the agent's op.mic path. The REST endpoint is authed with the Phase 1/2 PersonalAccessToken + Topology scope. RF safety is structural: the diagnostic never issues a control-plane PTT/key command, so A–D stay purely digital.

**Tech Stack:** Python 3.14, Django 6.0, DRF 3.17, Django Channels (InMemoryChannelLayer in tests), GStreamer `gst-launch-1.0` subprocesses (seams injected in tests), pure-Python S16LE DSP (struct), Node pure-logic JS tests for the browser layer.

**Spec:** `docs/superpowers/specs/2026-10-02-audio-path-diagnostics-design.md`

## Global Constraints

- **RF safety (non-negotiable):** the diagnostic MUST NOT request keying. No code path here may send a control-plane `command` with `capability == "ptt"` or otherwise key the SA818. Starting the audio TX bridge (digital inject into the PipeWire sink → USB) is permitted; keying the carrier is not. Every inject/measure path ends at D (the ALSA/UAC2 digital edge).
- **Server is a dumb relay:** no decode/re-encode of live Opus. The server originates the U reference only by replaying a **pre-encoded** Opus fixture as §5.3 media frames; it never runs an Opus encoder at runtime.
- **dBFS convention (uniform at every point):** `level_dbfs` is the **peak** dBFS of a signal relative to digital full scale. For S16LE, full scale = `32767`. `peak_dbfs = 20*log10(peak/32767)`, `rms_dbfs = 20*log10(rms/32767)`. For WebAudio float samples (−1.0..1.0) full scale = `1.0`. Silence reports `-inf` as the JSON value `null` with a companion `silent: true` — never a divide-by-zero.
- **Calibration convention:** reference = sine, default `freq_hz = 1000`, `level_dbfs = -20.0` (peak), `window_ms = 300`, with a `settle_ms = 200` lead-in before the measurement window. These are module-level named defaults, overridable per request.
- **Uniform schema** (machine-readable, AI-drivable):
  - Tap report: `{"point": str, "format": {"rate": int, "channels": int}, "rms_dbfs": float|null, "peak_dbfs": float|null, "window_ms": int, "silent": bool, "computed": bool, "static_gains": {...}?}`.
  - Inject request: `{"point": "U"|"T1"|"C", "signal": {"kind": "sine"|"wav", "freq_hz": int?, "level_dbfs": float, "duration_ms": int}}`.
  - Run report: `{"anchor": str, "reference": {...}, "taps": [tap_report...], "stages": [{"from": str, "to": str, "delta_db": float, "expected_db": float?, "note": str}], "verdict": str, "static_gains": {...}}`.
- **Dependency rule:** newest stable versions only; no new runtime deps are required by this plan (reuse struct/gst/channels already present).
- **Comment rule (Django templates):** `{% comment %}`, never multi-line `{# #}` (not expected to apply; no new templates planned).
- **Test layout:** Python tests at top-level `tests/test_*.py`; JS pure-logic at `tests/js/*.test.mjs` run via the existing `tests/test_audio_logic_js.py` Node harness. Run with `python -m pytest -q`.

## Review Focus

- **Agent offline / no agent WS connected when a run is requested** → the REST endpoint must return a clean `503`-style JSON error (`{"detail": "station agent not connected"}`), not hang forever or 500. Pinned in Task 9.
- **Agent result never arrives (gst hang, lost frame)** → the orchestrator's `channel_layer.receive` must be bounded by a timeout and surface `{"detail": "diagnostic timed out"}`. Pinned in Task 9.
- **Silent / empty capture** (no signal, pipeline error-exits with empty stdout) → dBFS math must yield `rms_dbfs=null, silent=true`, never raise or report `0.0`/`-inf` as a number. Pinned in Task 1.
- **Out-of-scope / applicant / DeviceKey principal hits the endpoint** → topology scope + membership gate must 404/403 exactly like the Phase 1/2 read API (no diagnostic run started for an unauthorized caller). Pinned in Task 9.
- **A malformed or hostile `diag_command` reaches the agent** (bad anchor, non-int slot, absurd duration) → the agent must reject with a `diag_result` error and never start a pipeline or an unbounded capture; absolute duration is clamped. Pinned in Task 5.

---

## File Structure

**Agent (`station_agent/audio/`)**
- `diagnostics.py` (new): pure DSP (`rms_peak_dbfs`, `generate_sine_pcm`), argv builders (`build_measured_inject_argv`, `build_measured_tx_argv`, `build_reverse_tap_argv`), orchestration (`run_diagnostic`), static-gain collection helper. Mirrors `selftest.py` seam style.
- `engine.py` (modify): add `on_diag_command(command) -> dict` returning a result payload; reuse `_to_thread`.
- `ws_client.py` (modify): dispatch `diag_command` → `engine.on_diag_command` → emit `diag_result` (carry `request_id`).
- `router_backend.py` (modify): add `get_volume(node) -> float | None` (parse `wpctl get-volume`) and `tx_sink_node(slot) -> str | None` helper for the device sink whose volume is the 0.40 stage.
- `__main__.py` (modify): add `selftest audio-diag` subcommand (local, on-target manual run).

**Server (`apps/audio/`)**
- `diagnostics.py` (new): pure report/verdict assembly (`build_run_report`), reference-fixture loader + frame packer (`load_reference_frames`, `iter_media_frames`), constants.
- `consumers.py` (modify, `AgentAudioConsumer`): `audio_diag_command` channel handler (push JSON to agent, remember `request_id`→`reply_channel`); `receive` handles `diag_result` → route to reply channel; `audio_ref_media` handler to replay U reference frames.
- `orchestrator.py` (new, `apps/audio/`): `run_headless_diagnostic(station, anchor, signal, taps) -> dict` — the async coroutine that drives one run over the channel layer with a bounded reply wait.
- `data/ref_1khz_-20dbfs_16k.opusframes` (new): committed pre-encoded Opus reference (length-prefixed packets).
- `scripts/gen_reference.py` (new, repo `scripts/` or `apps/audio/`): one-shot generator that produced the fixture (documented, not run in CI).

**Server REST (`apps/api/`)**
- `views.py` or new `diagnostics_views.py` (modify/new): `StationAudioDiagnosticView` (APIView, PAT+Topology), wired in `apps/api/urls.py` under `v1/stations/<pk>/audio-diagnostics/`.

**Browser (`static/js/`)**
- `audio-logic.js` (modify): pure `rmsToDbfs(rms)`, `dbfsToAmplitude(dbfs)`, `buildTapReport(...)`, `captureConstraints(track)` — unit-tested from Node.
- `mic-worklet.js` (modify): on-demand dBFS report (T1, worklet output) + inject mode flag.
- `audio-panel.js` (modify): T0 tap (MediaStreamSource post-getUserMedia), oscillator inject source swap, report static capture constraints.

**Tests**: `tests/test_audio_diagnostics_dsp.py`, `tests/test_audio_diagnostics_agent.py`, `tests/test_audio_diag_engine.py`, `tests/test_audio_diag_ws.py`, `tests/test_audio_diag_report.py`, `tests/test_audio_diag_consumer.py`, `tests/test_audio_diag_endpoint.py`, `tests/test_audio_reference_fixture.py`, extend `tests/js/audio-logic.test.mjs`.

**Docs**: resolve spec open-points; add `docs/audio-diagnostics-usage.md` (AI-driven run recipe).

---

## Task 1: Agent DSP core — dBFS + reference generation

**Files:**
- Create: `station_agent/audio/diagnostics.py`
- Test: `tests/test_audio_diagnostics_dsp.py`

**Interfaces:**
- Produces:
  - `FULL_SCALE_S16 = 32767`
  - `rms_peak_dbfs(pcm: bytes) -> tuple[float | None, float | None, bool]` — returns `(rms_dbfs, peak_dbfs, silent)`. Empty/all-zero PCM → `(None, None, True)`.
  - `generate_sine_pcm(freq_hz: int, level_dbfs: float, duration_ms: int, rate: int) -> bytes` — S16LE mono sine whose **peak** equals `level_dbfs` dBFS.
  - `REF_FREQ_HZ = 1000`, `REF_LEVEL_DBFS = -20.0`, `REF_WINDOW_MS = 300`, `REF_SETTLE_MS = 200`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_audio_diagnostics_dsp.py
import math
import struct

from station_agent.audio import diagnostics as d


def _sine_s16(freq, peak_amp, dur_ms, rate):
    n = int(rate * dur_ms / 1000)
    return struct.pack(
        f"<{n}h",
        *[max(-32768, min(32767, int(peak_amp * math.sin(2 * math.pi * freq * i / rate)))) for i in range(n)],
    )


def test_full_scale_sine_is_0_dbfs_peak():
    pcm = _sine_s16(1000, 32767, 300, 8000)
    rms, peak, silent = d.rms_peak_dbfs(pcm)
    assert silent is False
    assert peak == -0.0 or abs(peak) < 0.1          # full-scale peak ~ 0 dBFS
    assert abs(rms - (-3.01)) < 0.3                  # sine RMS is ~ -3 dBFS below peak


def test_minus20_sine_peaks_at_minus20():
    pcm = _sine_s16(1000, 32767 * 10 ** (-20 / 20), 300, 8000)
    rms, peak, silent = d.rms_peak_dbfs(pcm)
    assert abs(peak - (-20.0)) < 0.3


def test_silence_reports_none_not_zero():
    rms, peak, silent = d.rms_peak_dbfs(b"\x00\x00" * 1000)
    assert silent is True and rms is None and peak is None


def test_empty_pcm_is_silent():
    assert d.rms_peak_dbfs(b"") == (None, None, True)


def test_generate_sine_round_trips_to_requested_level():
    pcm = d.generate_sine_pcm(1000, -20.0, 300, 16000)
    rms, peak, silent = d.rms_peak_dbfs(pcm)
    assert silent is False
    assert abs(peak - (-20.0)) < 0.5
    assert len(pcm) == int(16000 * 300 / 1000) * 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_audio_diagnostics_dsp.py -q`
Expected: FAIL (module/attributes missing).

- [ ] **Step 3: Implement the DSP core**

```python
# station_agent/audio/diagnostics.py  (top of file)
"""Audio-path diagnostics: calibrated reference generation, dBFS metering, and
measured inject/tap pipelines for the TX chain (Spec: audio-path-diagnostics).

Pure DSP + argv builders are unit-tested; the subprocess capture/spawn seams are
injected (mirrors ``selftest.py``) so CI needs no GStreamer/PipeWire.

RF SAFETY: nothing here keys the SA818. Inject/measure end at the digital
ALSA/UAC2 edge (D); carrier keying is a separate control-plane action.
"""
from __future__ import annotations

import math
import struct

FULL_SCALE_S16 = 32767

REF_FREQ_HZ = 1000
REF_LEVEL_DBFS = -20.0
REF_WINDOW_MS = 300
REF_SETTLE_MS = 200


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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_audio_diagnostics_dsp.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/diagnostics.py tests/test_audio_diagnostics_dsp.py
git commit -m "feat(agent): audio-diag DSP core — dBFS metering + calibrated sine"
```

---

## Task 2: Agent measured inject/tap argv builders

**Files:**
- Modify: `station_agent/audio/diagnostics.py`
- Test: `tests/test_audio_diagnostics_agent.py` (argv section)

**Interfaces:**
- Consumes: constants from Task 1.
- Produces (pure functions returning `list[str]`):
  - `MEAS_FD = 3` — the extra fd gst-launch writes measurement PCM to.
  - `build_measured_inject_argv(tx_node: str, freq_hz: int, level_dbfs: float, rate: int) -> list[str]` — anchor **C**: `audiotestsrc` live sine → Opus roundtrip → `tee` splitting to `pipewiresink(tx_node)` **and** `fdsink fd=3` (S16LE). `volume` element sets the source to `level_dbfs`.
  - `build_measured_tx_argv(tx_node: str, port: int, rate: int) -> list[str]` — anchor **U**: the production `build_tx_argv` shape (udpsrc→jitterbuffer→opusdec) with a `tee` adding the `fdsink fd=3` measurement branch alongside `pipewiresink(tx_node)`.
  - `build_reverse_tap_argv(tap: str, rate: int, duration: float) -> list[str]` — optional sim/bench loopback capture (delegates to the proven `selftest.build_tx_capture_argv` shape).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_audio_diagnostics_agent.py
from station_agent.audio import diagnostics as d


def test_measured_inject_has_tee_sink_and_measfd():
    argv = d.build_measured_inject_argv("oe5xrx.slot1.tx", 1000, -20.0, 16000)
    s = " ".join(argv)
    assert "audiotestsrc" in s and "wave=sine" in s and "freq=1000" in s
    assert "opusenc" in s and "opusdec" in s        # real roundtrip, mirrors mic path
    assert "tee" in s
    assert "pipewiresink" in s and "target-object=oe5xrx.slot1.tx" in s
    assert f"fd={d.MEAS_FD}" in s                     # synchronous measurement branch
    assert "is-live=true" in s


def test_measured_tx_binds_loopback_and_taps():
    argv = d.build_measured_tx_argv("oe5xrx.slot1.tx", 47000, 16000)
    s = " ".join(argv)
    assert "udpsrc" in s and "address=127.0.0.1" in s and "port=47000" in s
    assert "rtpjitterbuffer" in s and "opusdec" in s
    assert "tee" in s and f"fd={d.MEAS_FD}" in s
    assert "pipewiresink" in s and "target-object=oe5xrx.slot1.tx" in s
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_audio_diagnostics_agent.py -q`
Expected: FAIL (builders missing).

- [ ] **Step 3: Implement the builders**

```python
# station_agent/audio/diagnostics.py  (append)
from station_agent.audio import selftest as _selftest

MEAS_FD = 3
_RTP_PT = 96
_LOOPBACK = "127.0.0.1"


def build_measured_inject_argv(tx_node: str, freq_hz: int, level_dbfs: float, rate: int) -> list[str]:
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_audio_diagnostics_agent.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/diagnostics.py tests/test_audio_diagnostics_agent.py
git commit -m "feat(agent): audio-diag measured inject/tx argv builders (tee→measfd)"
```

---

## Task 3: Agent static-gain collection (sink volume)

**Files:**
- Modify: `station_agent/audio/router_backend.py`
- Modify: `station_agent/audio/diagnostics.py`
- Test: `tests/test_audio_diagnostics_agent.py` (static-gains section)

**Interfaces:**
- Consumes: `PipeWireRouterBackend._safe_run`, `resolve_node`.
- Produces:
  - `PipeWireRouterBackend.get_volume(node: str) -> float | None` — parses `wpctl get-volume <node>` (`"Volume: 0.40"` → `0.40`); `None` on failure.
  - `diagnostics.collect_static_gains(backend, slot: int) -> dict` — `{"sink_volume_linear": float|null, "sink_volume_db": float|null, "resample": "48k<->8k", "note": str}`. `sink_volume_db = 20*log10(linear)`.
  - `diagnostics.parse_wpctl_volume(text: str) -> float | None` (pure, so the parse is unit-tested without a subprocess).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_audio_diagnostics_agent.py  (append)
import math

from station_agent.audio import diagnostics as d


def test_parse_wpctl_volume():
    assert d.parse_wpctl_volume("Volume: 0.40") == 0.40
    assert d.parse_wpctl_volume("Volume: 1.00 [MUTED]") == 1.00
    assert d.parse_wpctl_volume("garbage") is None


def test_collect_static_gains_reports_sink_db():
    class FakeBackend:
        def resolve_node(self, slot, direction):
            return "oe5xrx.slot1.tx"
        def tx_sink_node(self, slot):
            return "FM.Mono"
        def get_volume(self, node):
            return 0.40
    g = d.collect_static_gains(FakeBackend(), 1)
    assert g["sink_volume_linear"] == 0.40
    assert abs(g["sink_volume_db"] - (20 * math.log10(0.40))) < 0.01


def test_collect_static_gains_tolerates_missing_volume():
    class FakeBackend:
        def resolve_node(self, slot, direction): return "n"
        def tx_sink_node(self, slot): return None
        def get_volume(self, node): return None
    g = d.collect_static_gains(FakeBackend(), 1)
    assert g["sink_volume_linear"] is None and g["sink_volume_db"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_audio_diagnostics_agent.py -q -k static or volume`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# station_agent/audio/diagnostics.py  (append)
import re as _re

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
```

```python
# station_agent/audio/router_backend.py  (add methods to PipeWireRouterBackend)
    def get_volume(self, node: str) -> float | None:
        res = self._safe_run(["wpctl", "get-volume", node])
        if res is None or res.returncode != 0:
            return None
        from station_agent.audio.diagnostics import parse_wpctl_volume
        return parse_wpctl_volume(res.stdout)

    def tx_sink_node(self, slot: int) -> str | None:
        # The device sink that carries the 0.40 volume stage is the TX node's
        # target; on this platform the TX inject node IS that sink.
        return self.resolve_node(slot, "tx")
```

Note: if `router_backend.py` defines a `Protocol`/ABC for the backend near lines 60–65, add `get_volume` and `tx_sink_node` signatures there too so the interface stays declared.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_audio_diagnostics_agent.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/diagnostics.py station_agent/audio/router_backend.py tests/test_audio_diagnostics_agent.py
git commit -m "feat(agent): audio-diag static-gain collection (sink volume via wpctl)"
```

---

## Task 4: Agent diagnostic orchestration (`run_diagnostic`)

**Files:**
- Modify: `station_agent/audio/diagnostics.py`
- Test: `tests/test_audio_diagnostics_agent.py` (run section)

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `MAX_DURATION_MS = 5000` (absolute clamp).
  - `run_diagnostic(*, anchor, slot, signal, backend, spawn=..., read_measfd=..., port_allocator=None, reverse_tap=None) -> dict`
    - `anchor == "C"`: spawn `build_measured_inject_argv`, read the measurement fd for `settle_ms+window_ms`, compute C from the windowed PCM, derive D from `C + sink_volume_db`.
    - `anchor == "U"`: spawn `build_measured_tx_argv` on an allocated UDP port; return the `port` plus a `feed(opus_bytes)` is **not** this function's job — for U the engine wires the TxBridge-equivalent and the server feeds frames (see Task 5). `run_diagnostic` for U measures the fd and returns taps once `done`.
    - Returns `{"anchor": anchor, "reference": {"freq_hz","level_dbfs","window_ms"}, "taps": [tap_dict...], "static_gains": {...}}` where each `tap_dict` matches the Global-Constraints tap schema; C is `computed:false`, D is `computed:true` unless a real `reverse_tap` measured it.
  - `seam signatures`: `spawn(argv) -> proc` (proc exposes `.stdout`-like read via the measfd handed back by a `read_measfd(proc, nbytes, timeout) -> bytes` seam); default seams use `subprocess.Popen(..., pass_fds=(MEAS_FD,))` with `stdout`/a pipe on fd 3.

- [ ] **Step 1: Write the failing test** (anchor C, fully seamed — no real gst)

```python
# tests/test_audio_diagnostics_agent.py  (append)
from station_agent.audio import diagnostics as d


def test_run_diagnostic_anchor_c_reports_c_and_derived_d():
    ref_pcm = d.generate_sine_pcm(1000, -20.0, 300, 16000)

    class FakeBackend:
        def resolve_node(self, slot, direction): return "oe5xrx.slot1.tx"
        def tx_sink_node(self, slot): return "FM.Mono"
        def get_volume(self, node): return 0.40

    spawned = {}
    def fake_spawn(argv):
        spawned["argv"] = argv
        return object()
    def fake_read(proc, nbytes, timeout):
        return ref_pcm

    rep = d.run_diagnostic(
        anchor="C", slot=1,
        signal={"kind": "sine", "freq_hz": 1000, "level_dbfs": -20.0, "duration_ms": 500},
        backend=FakeBackend(), spawn=fake_spawn, read_measfd=fake_read,
    )
    assert rep["anchor"] == "C"
    taps = {t["point"]: t for t in rep["taps"]}
    assert abs(taps["C"]["peak_dbfs"] - (-20.0)) < 0.5
    assert taps["C"]["computed"] is False
    # D = C + 20log10(0.40) ~= C - 7.96 dB, flagged computed
    assert taps["D"]["computed"] is True
    assert abs(taps["D"]["rms_dbfs"] - (taps["C"]["rms_dbfs"] - 7.96)) < 0.1
    assert rep["static_gains"]["sink_volume_linear"] == 0.40


def test_run_diagnostic_clamps_absurd_duration():
    class FakeBackend:
        def resolve_node(self, s, d_): return "n"
        def tx_sink_node(self, s): return None
        def get_volume(self, n): return None
    captured = {}
    def fake_read(proc, nbytes, timeout):
        captured["timeout"] = timeout
        return b"\x00\x00" * 10
    d.run_diagnostic(anchor="C", slot=1,
                     signal={"kind": "sine", "level_dbfs": -20.0, "duration_ms": 10_000_000},
                     backend=FakeBackend(), spawn=lambda a: object(), read_measfd=fake_read)
    assert captured["timeout"] <= (d.MAX_DURATION_MS / 1000) + 1.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_audio_diagnostics_agent.py -q -k run_diagnostic`
Expected: FAIL.

- [ ] **Step 3: Implement `run_diagnostic`** (with default subprocess seams)

```python
# station_agent/audio/diagnostics.py  (append)
import logging as _logging
import os as _os
import subprocess as _subprocess

_log = _logging.getLogger(__name__)
MAX_DURATION_MS = 5000


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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_audio_diagnostics_agent.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/diagnostics.py tests/test_audio_diagnostics_agent.py
git commit -m "feat(agent): audio-diag run_diagnostic — anchor C/U, derived D, duration clamp"
```

---

## Task 5: Agent engine + WS dispatch (`diag_command` → `diag_result`)

**Files:**
- Modify: `station_agent/audio/engine.py`
- Modify: `station_agent/audio/ws_client.py`
- Test: `tests/test_audio_diag_engine.py`, `tests/test_audio_diag_ws.py`

**Interfaces:**
- Consumes: `diagnostics.run_diagnostic`, existing `AudioEngine._to_thread`, `TxBridge` for U.
- Produces:
  - `AudioEngine.on_diag_command(command: dict) -> dict` — validates `{anchor, slot, signal, request_id}`; for C/U calls `run_diagnostic` off-thread; returns a `diag_result` payload `{"v":1,"type":"diag_result","request_id":rid, ...report}` or `{...,"error":str}`. Rejects bad anchor / non-int slot without spawning. For anchor U it starts a diagnostic TX bridge via the engine (so inbound op.mic frames feed it) **without** touching PTT/gate, measures, then tears it down.
  - `ws_client.AudioClient._dispatch`: `elif mtype == "diag_command": await self._emit_json(await self._engine.on_diag_command(msg))`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_audio_diag_engine.py
import asyncio

from station_agent.audio.engine import AudioEngine


class FakeBackend:
    def list_audio_slots(self): return [1]
    def resolve_node(self, slot, direction): return "oe5xrx.slot1.tx"
    def tx_sink_node(self, slot): return "FM.Mono"
    def get_volume(self, node): return 0.40


def _engine():
    sent = []
    async def emit_json(m): sent.append(m)
    def emit_binary(b): pass
    eng = AudioEngine(FakeBackend(), emit_json=emit_json, emit_binary=emit_binary)
    return eng, sent


def test_on_diag_command_rejects_bad_anchor():
    eng, _ = _engine()
    res = asyncio.get_event_loop().run_until_complete(
        eng.on_diag_command({"anchor": "Z", "slot": 1, "request_id": "r1",
                             "signal": {"level_dbfs": -20.0}})
    )
    assert res["type"] == "diag_result" and res["request_id"] == "r1"
    assert "error" in res


def test_on_diag_command_rejects_non_int_slot():
    eng, _ = _engine()
    res = asyncio.get_event_loop().run_until_complete(
        eng.on_diag_command({"anchor": "C", "slot": "x", "request_id": "r2",
                             "signal": {"level_dbfs": -20.0}})
    )
    assert "error" in res


def test_on_diag_command_anchor_c_returns_report(monkeypatch):
    from station_agent.audio import diagnostics
    eng, _ = _engine()
    monkeypatch.setattr(diagnostics, "run_diagnostic",
                        lambda **kw: {"anchor": "C", "taps": [], "static_gains": {}})
    res = asyncio.get_event_loop().run_until_complete(
        eng.on_diag_command({"anchor": "C", "slot": 1, "request_id": "r3",
                             "signal": {"level_dbfs": -20.0}})
    )
    assert res["type"] == "diag_result" and res["request_id"] == "r3"
    assert res["anchor"] == "C"
```

```python
# tests/test_audio_diag_ws.py
import asyncio

from station_agent.audio.ws_client import AudioClient


class _Cfg:
    server_url = "https://x"; station_id = 1; ed25519_key_path = "/nonexistent"


def test_dispatch_routes_diag_command(monkeypatch):
    # Build a client without loading a key (patch load_private_key).
    import station_agent.audio.ws_client as m
    monkeypatch.setattr(m, "load_private_key", lambda p: object())
    c = AudioClient(_Cfg())

    class FakeEngine:
        async def on_diag_command(self, msg):
            return {"v": 1, "type": "diag_result", "request_id": msg["request_id"]}
    c._engine = FakeEngine()
    out = []
    async def fake_emit(m): out.append(m)
    c._send_json = fake_emit
    asyncio.get_event_loop().run_until_complete(
        c._dispatch('{"type":"diag_command","request_id":"r9","anchor":"C","slot":1,"signal":{}}')
    )
    assert out and out[0]["type"] == "diag_result" and out[0]["request_id"] == "r9"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_audio_diag_engine.py tests/test_audio_diag_ws.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# station_agent/audio/engine.py  (add method)
    async def on_diag_command(self, command: dict) -> dict:
        from station_agent.audio import diagnostics
        rid = command.get("request_id")
        anchor = command.get("anchor")
        slot = command.get("slot")
        signal = command.get("signal") or {}
        base = {"v": 1, "type": "diag_result", "request_id": rid}
        if anchor not in ("C", "U"):
            return {**base, "error": f"unsupported anchor {anchor!r}"}
        if not isinstance(slot, int) or isinstance(slot, bool):
            return {**base, "error": "slot must be an int"}
        try:
            report = await self._to_thread(
                lambda: diagnostics.run_diagnostic(
                    anchor=anchor, slot=slot, signal=signal, backend=self._backend,
                    rate=self.registry.mic_rate,
                )
            )
        except Exception as exc:  # noqa: BLE001 — a diag failure must not kill the WS loop
            logger.exception("engine: diagnostic run failed")
            return {**base, "error": f"{type(exc).__name__}: {exc}"}
        return {**base, **report}
```

```python
# station_agent/audio/ws_client.py  (_dispatch, add branch before the else)
        elif mtype == "diag_command":
            result = await self._engine.on_diag_command(msg)
            await self._send_json(result)
```

Note: `AudioEngine.__init__` stores `self._backend`; confirm it is kept (it is). For anchor U the engine-level TX-bridge wiring is additive — if time-boxed, U may initially run the measured-TX pipeline standalone in `run_diagnostic` and the server feeds the allocated port; the C anchor is the CI-complete path. Keep U behind the same `on_diag_command` surface.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_audio_diag_engine.py tests/test_audio_diag_ws.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add station_agent/audio/engine.py station_agent/audio/ws_client.py tests/test_audio_diag_engine.py tests/test_audio_diag_ws.py
git commit -m "feat(agent): audio-diag engine command + WS dispatch (diag_command/diag_result)"
```

---

## Task 6: Server report + verdict assembly

**Files:**
- Create: `apps/audio/diagnostics.py`
- Test: `tests/test_audio_diag_report.py`

**Interfaces:**
- Produces:
  - `SINK_EXPECTED_DB = -7.96` (20·log10(0.40); documents the known stage).
  - `build_run_report(agent_report: dict) -> dict` — takes the agent's `{anchor, reference, taps, static_gains}` and adds `stages` (per-stage deltas with `expected_db` where known) and a human `verdict` string. Pure.
  - Verdict rules: if C is silent → `"no signal at C — inject/agent path broken"`. If C→D delta ≈ `sink_volume_db` (within 1 dB) and C is near full-scale (peak > −6 dBFS) → `"digital chain clean to D; C->D loss is the sink volume stage (<db>) — expected"`. If C peak is low (< −12 dBFS) → `"low level already at C — loss upstream (agent/opus or injected reference)"`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_audio_diag_report.py
from apps.audio import diagnostics as sd


def _agent_report(c_peak, c_rms, sink_db=-7.96):
    d_rms = None if c_rms is None else round(c_rms + sink_db, 2)
    d_peak = None if c_peak is None else round(c_peak + sink_db, 2)
    return {
        "anchor": "C",
        "reference": {"freq_hz": 1000, "level_dbfs": -20.0, "window_ms": 300},
        "taps": [
            {"point": "C", "rms_dbfs": c_rms, "peak_dbfs": c_peak, "silent": c_rms is None, "computed": False},
            {"point": "D", "rms_dbfs": d_rms, "peak_dbfs": d_peak, "silent": d_rms is None, "computed": True},
        ],
        "static_gains": {"sink_volume_linear": 0.40, "sink_volume_db": sink_db},
    }


def test_report_has_stage_delta_and_expected():
    rep = sd.build_run_report(_agent_report(-3.0, -6.0))
    stages = {(s["from"], s["to"]): s for s in rep["stages"]}
    cd = stages[("C", "D")]
    assert abs(cd["delta_db"] - (-7.96)) < 0.1
    assert abs(cd["expected_db"] - sd.SINK_EXPECTED_DB) < 0.1


def test_verdict_clean_chain_blames_sink_volume():
    rep = sd.build_run_report(_agent_report(-3.0, -6.0))
    assert "sink volume" in rep["verdict"].lower()


def test_verdict_silent_c_flags_broken_inject():
    rep = sd.build_run_report(_agent_report(None, None))
    assert "no signal" in rep["verdict"].lower()


def test_verdict_low_c_blames_upstream():
    rep = sd.build_run_report(_agent_report(-24.0, -27.0))
    assert "upstream" in rep["verdict"].lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_audio_diag_report.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement** `apps/audio/diagnostics.py` per the verdict rules above (pure functions; iterate adjacent taps to build `stages`, attach `expected_db = sink_volume_db` to the `C→D` stage).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_audio_diag_report.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/audio/diagnostics.py tests/test_audio_diag_report.py
git commit -m "feat(audio): server-side diag report + verdict assembly"
```

---

## Task 7: U reference fixture + frame packer

**Files:**
- Create: `apps/audio/data/ref_1khz_-20dbfs_16k.opusframes`
- Create: `scripts/gen_audio_reference.py`
- Modify: `apps/audio/diagnostics.py` (loader + frame iterator)
- Test: `tests/test_audio_reference_fixture.py`

**Interfaces:**
- Produces:
  - `scripts/gen_audio_reference.py` — offline: generate a 1 kHz −20 dBFS sine WAV, Opus-encode to 20 ms packets via `gst-launch`/`opusenc`, write length-prefixed packets (`uint16 len` + bytes) to the `.opusframes` file. Documented; not run in CI.
  - `apps/audio.diagnostics.load_reference_frames(path=DEFAULT_REF_PATH) -> list[bytes]` — reads length-prefixed Opus packets.
  - `apps/audio.diagnostics.iter_media_frames(frames, stream_ref, *, repeat, seq0=0) -> Iterator[bytes]` — wraps each Opus packet in a §5.3 media frame (reuse `station_agent.audio.frame.pack_frame`) with the op.mic `stream_ref`, advancing seq/ts; loops `repeat` times to cover the window.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_audio_reference_fixture.py
from apps.audio import diagnostics as sd
from station_agent.audio.frame import parse_frame


def test_fixture_loads_nonempty_opus_packets():
    frames = sd.load_reference_frames()
    assert len(frames) >= 10           # ~200ms+ of 20ms packets
    assert all(isinstance(f, bytes) and len(f) > 0 for f in frames)


def test_iter_media_frames_are_valid_s5_3_frames():
    frames = sd.load_reference_frames()
    out = list(sd.iter_media_frames(frames[:5], stream_ref=7, repeat=2))
    assert len(out) == 10
    seqs = []
    for data in out:
        mf = parse_frame(data)
        assert mf.stream_ref == 7
        seqs.append(mf.seq)
    assert seqs == sorted(seqs)         # monotonic seq across the repeat
```

- [ ] **Step 2: Run to verify it fails** — FAIL (fixture + functions missing).

- [ ] **Step 3: Implement** the generator script, generate the fixture (run the script once locally where gst is available; if gst is unavailable in the plan-execution env, generate a deterministic fixture by encoding with any available Opus encoder, or document the exact command and commit a checked-in artifact produced on the test station). Implement `load_reference_frames` + `iter_media_frames`.

  - If no Opus encoder is available to the executor, generate the fixture on test-station 211 and `scp` it in; the loader/iterator logic is what CI tests.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_audio_reference_fixture.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/audio/data/ scripts/gen_audio_reference.py apps/audio/diagnostics.py tests/test_audio_reference_fixture.py
git commit -m "feat(audio): U-anchor Opus reference fixture + s5.3 frame packer"
```

---

## Task 8: Server WS correlation (consumer handlers)

**Files:**
- Modify: `apps/audio/consumers.py` (`AgentAudioConsumer`)
- Test: `tests/test_audio_diag_consumer.py`

**Interfaces:**
- Consumes: existing `AgentAudioConsumer` group wiring.
- Produces (on `AgentAudioConsumer`):
  - `self._diag_replies: dict[str, str]` — `request_id` → reply channel name (init in `connect`).
  - `receive`: handle JSON `type == "diag_result"` → `channel_layer.send(self._diag_replies.pop(rid), {"type":"diag.reply","msg":msg})`.
  - channel handler `audio_diag_command(event)` → store `event["request_id"] → event["reply_channel"]`, then `self.send(text_data=json.dumps(event["command"]))`.
  - channel handler `audio_ref_media(event)` → `self.send(bytes_data=event["data"])` (replays a U reference frame to the agent; identical effect to a browser mic frame).

- [ ] **Step 1: Write failing test** (InMemoryChannelLayer, drive the consumer with a fake agent)

```python
# tests/test_audio_diag_consumer.py
import json

import pytest
from channels.testing import WebsocketCommunicator
# ... follow the existing tests/test_audio_consumer.py setup for auth/station fixtures ...
```

Model it on `tests/test_audio_consumer.py`: connect an authenticated `AgentAudioConsumer`, `group_send` an `audio.diag_command` with a `reply_channel` created via `get_channel_layer().new_channel()`, assert the agent receives the `diag_command` JSON; then have the test send a `diag_result` frame into the consumer and assert it lands on the reply channel via `channel_layer.receive`.

- [ ] **Step 2: Run to verify it fails** — FAIL.

- [ ] **Step 3: Implement** the three handlers + `receive` branch + `_diag_replies` init.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_audio_diag_consumer.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/audio/consumers.py tests/test_audio_diag_consumer.py
git commit -m "feat(audio): agent-consumer diag_command/diag_result correlation + ref media replay"
```

---

## Task 9: Orchestrator + REST endpoint

**Files:**
- Create: `apps/audio/orchestrator.py`
- Create: `apps/api/diagnostics_views.py`
- Modify: `apps/api/urls.py`
- Test: `tests/test_audio_diag_endpoint.py`

**Interfaces:**
- Consumes: `apps.audio.diagnostics.build_run_report`, `load_reference_frames`, `iter_media_frames`; `apps.stations.scoping.accessible_stations`; Phase 1/2 `PersonalAccessTokenAuthentication`, `TopologyScopedPermission`.
- Produces:
  - `apps/audio/orchestrator.py`: `async def run_headless_diagnostic(station_id, anchor, signal, *, timeout=15.0) -> dict` — build a reply channel, `group_send(agent_group, {"type":"audio.diag_command","command":{...,"request_id":rid},"reply_channel":ch,"request_id":rid})`; for anchor U also stream reference frames via `audio.ref_media`; `await channel_layer.receive(ch)` bounded by `asyncio.wait_for(timeout)`. Returns `build_run_report(result)` or raises `AgentNotConnected` / `DiagnosticTimeout`.
  - A presence check: if no agent is in `agent_group` (track via a known registry or a short probe), raise `AgentNotConnected`. Simplest: rely on the timeout + a quick "is the station online" check via the control registry/`Station.is_online`-style flag already used elsewhere; if offline, fail fast with `AgentNotConnected`.
  - `apps/api/diagnostics_views.py`: `StationAudioDiagnosticView(APIView)` with `authentication_classes=[PersonalAccessTokenAuthentication, SessionAuthentication]`, `permission_classes=[TopologyScopedPermission]`, `throttle_scope="api-token"`. `post(request, pk)`: resolve the station via `accessible_stations(request.user)` (404 if not in scope), parse+validate `anchor`/`signal` (default anchor `"U"`, default signal = calibration defaults), run the orchestrator via `async_to_sync`, map exceptions to `503`/`504` JSON, return the run report `200`.
  - URL: `path("v1/stations/<int:pk>/audio-diagnostics/", StationAudioDiagnosticView.as_view(), name="station-audio-diagnostics")`.

- [ ] **Step 1: Write failing tests** (mock the orchestrator so no real channel layer/agent is needed)

```python
# tests/test_audio_diag_endpoint.py
import pytest
from rest_framework.test import APIClient
# reuse PAT + station + topology fixtures from tests/test_api_read_stations.py / test_api_permission_scope.py


def _auth(client, raw_token):
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw_token}")


def test_in_scope_user_gets_report(db, monkeypatch, station_in_scope, pat_for_owner):
    from apps.audio import orchestrator
    async def fake_run(station_id, anchor, signal, **kw):
        return {"anchor": anchor, "verdict": "ok", "taps": [], "stages": [], "static_gains": {}}
    monkeypatch.setattr(orchestrator, "run_headless_diagnostic", fake_run)
    client = APIClient(); _auth(client, pat_for_owner)
    r = client.post(f"/api/v1/stations/{station_in_scope.pk}/audio-diagnostics/",
                    {"anchor": "U"}, format="json")
    assert r.status_code == 200 and r.json()["anchor"] == "U"


def test_out_of_scope_station_404(db, station_out_of_scope, pat_for_owner):
    client = APIClient(); _auth(client, pat_for_owner)
    r = client.post(f"/api/v1/stations/{station_out_of_scope.pk}/audio-diagnostics/",
                    {"anchor": "U"}, format="json")
    assert r.status_code == 404


def test_applicant_forbidden(db, station_in_scope, pat_for_applicant):
    client = APIClient(); _auth(client, pat_for_applicant)
    r = client.post(f"/api/v1/stations/{station_in_scope.pk}/audio-diagnostics/",
                    {"anchor": "U"}, format="json")
    assert r.status_code == 403


def test_agent_offline_returns_503(db, monkeypatch, station_in_scope, pat_for_owner):
    from apps.audio import orchestrator
    async def fake_run(*a, **k): raise orchestrator.AgentNotConnected()
    monkeypatch.setattr(orchestrator, "run_headless_diagnostic", fake_run)
    client = APIClient(); _auth(client, pat_for_owner)
    r = client.post(f"/api/v1/stations/{station_in_scope.pk}/audio-diagnostics/",
                    {"anchor": "U"}, format="json")
    assert r.status_code == 503


def test_timeout_returns_504(db, monkeypatch, station_in_scope, pat_for_owner):
    from apps.audio import orchestrator
    async def fake_run(*a, **k): raise orchestrator.DiagnosticTimeout()
    monkeypatch.setattr(orchestrator, "run_headless_diagnostic", fake_run)
    client = APIClient(); _auth(client, pat_for_owner)
    r = client.post(f"/api/v1/stations/{station_in_scope.pk}/audio-diagnostics/",
                    {"anchor": "U"}, format="json")
    assert r.status_code == 504
```

Reuse/define the PAT + station + topology fixtures following `tests/test_api_permission_scope.py` and `tests/test_api_read_stations.py` (whichever defines `PersonalAccessToken` creation + an in-scope station assignment). If those helpers are not importable, create minimal fixtures in this test module mirroring them.

- [ ] **Step 2: Run to verify it fails** — FAIL.

- [ ] **Step 3: Implement** the orchestrator (exceptions `AgentNotConnected`, `DiagnosticTimeout`), the APIView with exception→status mapping (`AgentNotConnected`→503, `DiagnosticTimeout`→504), and the URL wiring.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_audio_diag_endpoint.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/audio/orchestrator.py apps/api/diagnostics_views.py apps/api/urls.py tests/test_audio_diag_endpoint.py
git commit -m "feat(api): POST /stations/{id}/audio-diagnostics — headless run, PAT+topology, 503/504 mapping"
```

---

## Task 10: Browser T0/T1 taps + inject

**Files:**
- Modify: `static/js/audio-logic.js`
- Modify: `static/js/mic-worklet.js`
- Modify: `static/js/audio-panel.js`
- Test: `tests/js/audio-logic.test.mjs` (extend)

**Interfaces:**
- Produces in `audio-logic.js` (pure, UMD-exported):
  - `rmsToDbfs(rms)` — float-sample RMS (0..1) → dBFS; `rms<=0` → `null`.
  - `dbfsToAmplitude(dbfs)` — inverse, for oscillator inject gain.
  - `buildTapReport(point, {rms, peak, rate, windowMs, constraints})` — the uniform tap schema object (float FS = 1.0).
  - `captureConstraintsFromSettings(settings)` — pick `autoGainControl`/`noiseSuppression`/`echoCancellation` from a `MediaTrackSettings`-like object into `static_gains`.
- `mic-worklet.js`: on a `{type:"diag_report"}` control message, compute windowed RMS/peak of the worklet output (T1) and post `{type:"diag_tap", point:"T1", rms, peak}`; on `{type:"diag_inject", on, ...}` swap to a synthesized sine (or mute mic) — **live operator only**.
- `audio-panel.js`: wire a MediaStreamSource AnalyserNode for T0 (post-getUserMedia), report `track.getSettings()` constraints, and expose an operator-only inject toggle. (DOM wiring is not unit-tested; the extracted math in `audio-logic.js` is.)

- [ ] **Step 1: Write failing JS tests** — extend `tests/js/audio-logic.test.mjs` with assertions:

```js
// full-scale float sine rms ~0.707 -> ~ -3 dBFS
assertClose(L.rmsToDbfs(0.7071), -3.01, 0.1);
assertEqual(L.rmsToDbfs(0), null);
assertClose(L.dbfsToAmplitude(-20), 0.1, 0.001);
const rep = L.buildTapReport("T1", {rms: 0.1, peak: 0.1, rate: 48000, windowMs: 300, constraints: {}});
assertEqual(rep.point, "T1");
assertEqual(rep.format.rate, 48000);
```

(Use the file's existing `assertClose`/`assertEqual` helpers; if absent, add tiny local helpers matching its style.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_audio_logic_js.py -q`
Expected: FAIL (new assertions).

- [ ] **Step 3: Implement** the pure functions in `audio-logic.js`, then the worklet + panel wiring.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_audio_logic_js.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add static/js/audio-logic.js static/js/mic-worklet.js static/js/audio-panel.js tests/js/audio-logic.test.mjs
git commit -m "feat(ui): browser T0/T1 dBFS taps + operator inject (pure logic unit-tested)"
```

---

## Task 11: CLI subcommand + usage docs + spec close-out

**Files:**
- Modify: `station_agent/__main__.py`
- Create: `docs/audio-diagnostics-usage.md`
- Modify: `docs/superpowers/specs/2026-10-02-audio-path-diagnostics-design.md` (resolve open points)
- Test: none new (CLI is a thin dispatch; covered by engine/diagnostics tests)

- [ ] **Step 1: Add `selftest audio-diag` subcommand** to `__main__.py` mirroring the existing `audio` subparser: `--slot`, `--anchor {C,U}`, `--freq`, `--level-dbfs`, `--duration-ms`; dispatch to a small `diagnostics.run_diagnostic(...)` + print the JSON report to stdout. This is the on-target manual runner for station 211.

- [ ] **Step 2: Write `docs/audio-diagnostics-usage.md`** — the AI-driven recipe: `POST /api/v1/stations/{id}/audio-diagnostics/ {"anchor":"U"}` with a Bearer PAT, read the returned delta table + verdict; the bisection loop (U vs C, interpret C→D sink stage); RF-safety note (no keying); on-station validation steps for station 211.

- [ ] **Step 3: Resolve the spec's "Offene Punkte"** — record the decisions: diagnostic messages ride the existing agent **audio** consumer (not a new channel); calibration = 1 kHz / −20 dBFS peak / 300 ms window / 200 ms settle; D is computed from C + measured sink volume on real HW (and optionally measured via reverse tap on sim/bench).

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all tasks green).

- [ ] **Step 5: Commit**

```bash
git add station_agent/__main__.py docs/audio-diagnostics-usage.md docs/superpowers/specs/2026-10-02-audio-path-diagnostics-design.md
git commit -m "feat(agent): selftest audio-diag CLI + usage docs + spec close-out"
```

---

## Self-Review Notes

- **Spec coverage:** T0/T1 (Task 10), C/D (Tasks 2/4), U/T1/C inject anchors (C in Task 4, U in Tasks 4/7/8/9, T1 in Task 10), server dumb-relay reference replay (Tasks 7/8), orchestrator + REST auth/scope (Task 9), uniform dBFS schema (Global Constraints + Tasks 1/4/6/10), static gains (Task 3), RF-safety (Global Constraints; no PTT path anywhere), AI-headless mode (Task 9 default anchor U). Server freed from a tap by construction (no server tap task) — matches spec §"Server ist per Konstruktion transparent".
- **Known honest gaps (on-station validation):** real gst/PipeWire capture numbers at C and the real sink-volume read on station 211; the committed Opus fixture ideally generated on-station. CI proves all logic; the PR is mergeable with on-station validation flagged for the user (per brief).
- **Type consistency:** tap dict schema identical across `station_agent.audio.diagnostics._tap`, `apps.audio.diagnostics.build_run_report` consumption, and `audio-logic.buildTapReport`. `request_id` threads command→result→reply-channel unchanged.
