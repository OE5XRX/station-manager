# TX Audio Leveling — station-manager (Baustein C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the TX path one level/deviation authority (agent DSP: band-pass → gate → compressor → makeup → limiter), clean browser capture, a per-station calibration number, role-gated + persisted SA818 filter capabilities, and a live TX modulation meter.

**Architecture:** The agent's `gst-launch` TX pipeline gains a DSP fragment built from a pure policy module (`tx_dsp.py`) and a pre-limiter F32 tap whose samples are run through the limiter's exact static curve in Python (`tx_meter.py`) to yield the transmitted level + gain reduction, pushed as `tx_meter` over the existing audio WS. The calibration ceiling is a nullable `Station` field, pushed in the heartbeat *response* into a thread-safe agent holder read at each TX-bridge start. Capability `write_role` is a central server policy map enforced in `ControlConsumer` and reflected read-only in the SSR widgets; role-gated values are persisted per station and re-applied by the server through the agent whenever an inventory frame shows drift.

**Tech Stack:** Django 6.0, Channels, Alpine.js, gst-launch-1.0 (audiofx `audiocheblimit`/`audiodynamic`, base `volume`/`tee`/`queue`/`fdsink`), Python 3.14 agent, Node for pure-JS tests.

**Spec:** `docs/superpowers/specs/2026-10-03-tx-audio-leveling-design.md`

## Global Constraints

- RF safety: nothing in this plan keys the SA818. DSP/metering/filter-set/calibration are purely digital (spec §7).
- Limiter MUST be the last DSP stage before `pipewiresink` (spec §3.2, §5).
- No new image dependency beyond gst built-ins; missing elements ⇒ graceful degradation, TX never breaks (spec §3.2).
- Calibration unset/out-of-range ⇒ clamp; default errs toward under-deviation (spec §4).
- FW stays role-agnostic; `write_role` is a server policy map; capabilities without an entry default to `operator` (spec §4a).
- Server-side enforcement at the set path is the real gate; read-only render is cosmetic (spec §4a).
- Tests top-level `tests/test_*.py`; JS pure logic in `tests/js/*.test.mjs` run via pytest wrapper; `python -m pytest -q`; `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .`.
- Django templates: never multi-line `{# #}` — use `{% comment %}`.
- Numeric inputs: `lang="en"` (DE-locale dot-decimal rule).
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Decisions taken in this plan (spec "decision points")

1. **§5 pre-emphasis default = "simpler fallback"**: SA818 pre-emphasis ON (its power-on state; FW shadow defaults to `SA818_FILTER_ALL`), agent flat. Reason: an agent-side pre-emphasis curve is **not expressible with gst built-ins via gst-launch** — `audiofirfilter`/`audioiirfilter` `kernel`/`a`/`b` are `GValueArray`s which gst-launch cannot deserialize (verified: `gst_value_deserialize_g_value_array: unimplemented`). The preferred config is a follow-up (needs appsrc/python-gi or a custom element).
2. **`audiodynamic` is memoryless (per-sample static curve, no attack/release/hang)** — verified via `gst-inspect-1.0`. So the spec's "hang time + slow release" for the gate is not achievable with built-ins; the gate is a low-threshold soft-knee expander (only affects near-silence), the compressor a soft-knee static curve (speech-processor style), the limiter a hard-knee high-ratio curve (= clipper at the ceiling; SA818 LPF ON band-limits the clip products — classic ham "clipper + splatter filter"). Envelope-based dynamics = follow-up.
3. **Calibration number = limiter ceiling in dBFS** (`Station.tx_audio_ceiling_dbfs`, nullable). Range `[-24.0, -3.0]`, default `-12.0`. Makeup gain is derived: `makeup_db = ceiling − headroom(3) − nominal_compressed_peak(−24)`.
4. **Re-apply of persisted filters is server-driven through the agent**: on every agent `inventory` frame the server compares the reported module state with the persisted value and sends a `command set` for any drift (covers agent reconnect, module reboot/SA818 power-cycle). Idempotent, no transition tracking. TX-start re-apply is not needed on top (a power-cycled SA818 always re-enumerates → inventory).
5. **Degraded mode gain = 1.0** (pass-through): without a limiter, any makeup gain risks over-deviation; spec §4 fail-safe wins over "fixed makeup gain".
6. **Image gap (follow-up, other repo):** the image installs only split gst subpackages; `audiocheblimit`/`audiodynamic` live in `gstreamer1.0-plugins-good-audiofx`, which is **not** in `oe5xrx-audio-system` RDEPENDS. Until linux-image adds it, stations run degraded (pass-through + meter). Reported to coordinator, not done here.
7. **Calibration editing is staff-only** (RF-safety relevant): `StationForm` drops the field for non-internal users; API exposes it read-only.

## Review Focus

- Meter reader stalls / slow consumer must never back-pressure TX audio → `queue leaky=downstream` before `fdsink`; test asserts it in argv (Task 3).
- `tx_meter` frames with junk/NaN/huge numbers from a compromised agent must not reach browsers unsanitized (Task 6 test).
- A role-gated set sent by a lock-holding operator crafted outside the UI (raw WS frame) must be refused and not relayed (Task 8 test).
- Heartbeat response with missing/garbage `tx_audio` (old server, null, string, NaN) must leave the agent at the safe default, never crash the heartbeat loop (Task 2 test).
- DSP pipeline that dies at startup (element present but misbehaving) must fall back to the plain pipeline, not leave TX silent (Task 4 test).

---

### Task 1: Station calibration field + clamp resolver + staff-only edit

**Files:**
- Create: `apps/stations/tx_audio.py`
- Modify: `apps/stations/models.py` (Station fields, after `hardware_revision`)
- Create: `apps/stations/migrations/0025_station_tx_audio_ceiling_dbfs.py` (via makemigrations)
- Modify: `apps/stations/forms.py` (StationForm), `apps/stations/views.py` (pass `user` to form at lines ~169/~209), `apps/api/read_serializers.py:40` (StationSerializer read-only field)
- Test: `tests/test_station_tx_audio_calibration.py`

**Interfaces:**
- Produces: `apps.stations.tx_audio.CEILING_DEFAULT_DBFS = -12.0`, `CEILING_MIN_DBFS = -24.0`, `CEILING_MAX_DBFS = -3.0`, `effective_ceiling_dbfs(value) -> float`, `Station.tx_audio_ceiling_dbfs: float | None`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_station_tx_audio_calibration.py
import math

import pytest

from apps.stations import tx_audio
from apps.stations.forms import StationForm


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, -12.0),
        (-12.0, -12.0),
        (-6.5, -6.5),
        (-30.0, -24.0),   # below range → clamp to the quiet end
        (0.0, -3.0),      # above range → clamp (never splatter)
        (5, -3.0),
        (float("nan"), -12.0),
        (float("inf"), -12.0),
        ("loud", -12.0),
        (True, -12.0),    # bool is not a calibration number
    ],
)
def test_effective_ceiling_clamps_and_defaults(raw, expected):
    assert tx_audio.effective_ceiling_dbfs(raw) == expected


def test_default_is_on_the_quiet_side_of_midrange():
    mid = (tx_audio.CEILING_MIN_DBFS + tx_audio.CEILING_MAX_DBFS) / 2
    assert tx_audio.CEILING_DEFAULT_DBFS <= mid
    assert math.isfinite(tx_audio.CEILING_DEFAULT_DBFS)


@pytest.mark.django_db
def test_station_field_nullable_default_none(station_factory):
    s = station_factory()
    assert s.tx_audio_ceiling_dbfs is None


@pytest.mark.django_db
def test_form_exposes_field_only_to_internal(user_factory):
    staff = user_factory(membership_level="staff")
    member = user_factory(membership_level="member")
    assert "tx_audio_ceiling_dbfs" in StationForm(user=staff).fields
    assert "tx_audio_ceiling_dbfs" not in StationForm(user=member).fields
    assert "tx_audio_ceiling_dbfs" not in StationForm().fields  # no user → safe default


@pytest.mark.django_db
def test_form_rejects_out_of_range_for_staff(user_factory):
    staff = user_factory(membership_level="staff")
    f = StationForm(
        data={"name": "S", "callsign": "OE5XRX", "tx_audio_ceiling_dbfs": "-40"}, user=staff
    )
    assert not f.is_valid()
    assert "tx_audio_ceiling_dbfs" in f.errors
```

Before writing, grep `tests/conftest.py` for the existing station/user fixtures (`grep -n "def .*factory\|@pytest.fixture" tests/conftest.py`) and adapt the fixture names/kwargs to what exists (e.g. create via `Station.objects.create(...)` / `User.objects.create_user(...)` if no factory exists). Also grep the StationForm required fields so the `data=` dict in the last test only fails on the ceiling field (assert the ceiling key is in `f.errors`, which is what matters).

- [ ] **Step 2: Run — expect ImportError/FAIL**

Run: `python -m pytest -q tests/test_station_tx_audio_calibration.py`

- [ ] **Step 3: Implement**

```python
# apps/stations/tx_audio.py
"""TX-audio calibration resolution (spec 2026-10-03 §4).

One number per station: the agent limiter ceiling in dBFS, which maps to the target FM
deviation at that board's SA818. Unset or out-of-range values are clamped; the default errs
toward under-deviation so an uncalibrated station can never splatter.
"""

import math

CEILING_DEFAULT_DBFS = -12.0
CEILING_MIN_DBFS = -24.0
CEILING_MAX_DBFS = -3.0


def effective_ceiling_dbfs(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return CEILING_DEFAULT_DBFS
    if not math.isfinite(value):
        return CEILING_DEFAULT_DBFS
    return float(min(CEILING_MAX_DBFS, max(CEILING_MIN_DBFS, value)))
```

In `Station` (after `hardware_revision`):

```python
    tx_audio_ceiling_dbfs = models.FloatField(
        _("TX audio ceiling (dBFS)"),
        null=True,
        blank=True,
        validators=[MinValueValidator(-24.0), MaxValueValidator(-3.0)],
        help_text=_(
            "Per-board calibration: agent limiter ceiling that yields the target FM "
            "deviation. Empty = conservative default (-12 dBFS)."
        ),
    )
```
(import `MinValueValidator, MaxValueValidator` from `django.core.validators` if not already imported; use the constants from `tx_audio` instead of literals to stay DRY.)

`StationForm`: add `"tx_audio_ceiling_dbfs"` to `Meta.fields`, widget `forms.NumberInput(attrs={"class": "form-control", "step": "0.5", "lang": "en"})`, and:

```python
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # RF-safety calibration: only Vereins-Staff/Admin may change it.
        if user is None or not getattr(user, "is_internal", False):
            self.fields.pop("tx_audio_ceiling_dbfs", None)
```
In both StationForm views add `get_form_kwargs` returning `{**super().get_form_kwargs(), "user": self.request.user}`.
`StationSerializer` (read): add `"tx_audio_ceiling_dbfs"` to fields and `read_only_fields`. Do NOT add it to `StationWriteSerializer`.

Run `python manage.py makemigrations stations -n station_tx_audio_ceiling_dbfs`.

- [ ] **Step 4: Run tests + full stations/api suites**

Run: `python -m pytest -q tests/test_station_tx_audio_calibration.py tests -k "station or api" -x`
Expected: PASS (fix any OpenAPI schema snapshot test that lists Station fields).

- [ ] **Step 5: Commit** — `feat(stations): per-station TX audio ceiling calibration field`

---

### Task 2: Heartbeat response pushes the effective ceiling; agent holder

**Files:**
- Modify: `apps/api/views.py` (`HeartbeatView.post` final `Response`, ~line 136)
- Create: `station_agent/audio/tx_settings.py`
- Modify: `station_agent/heartbeat.py` (`send_heartbeat`), `station_agent/agent.py` (~line 600-625: create holder, pass to `send_heartbeat` and `BridgeFactory`)
- Test: `tests/test_heartbeat_tx_audio.py`

**Interfaces:**
- Consumes: `apps.stations.tx_audio.effective_ceiling_dbfs`.
- Produces: heartbeat 200 body `{"status": "ok", "tx_audio": {"ceiling_dbfs": <float>, "calibrated": <bool>}}`; `station_agent.audio.tx_settings.TxAudioSettings` with `.ceiling_dbfs -> float` (property), `.update_from_heartbeat(body) -> None`; `send_heartbeat(http_client, config=None, tx_settings=None) -> bool`. Agent-side constants mirror the server: `CEILING_DEFAULT_DBFS=-12.0`, `CEILING_MIN_DBFS=-24.0`, `CEILING_MAX_DBFS=-3.0`, `clamp_ceiling(value) -> float` (identical semantics to `effective_ceiling_dbfs`; the agent clamps again — defence in depth, it must not trust the wire).

- [ ] **Step 1: Failing tests**

```python
# tests/test_heartbeat_tx_audio.py
import math
import threading
from types import SimpleNamespace

import pytest

from station_agent.audio import tx_settings as ts
from station_agent.heartbeat import send_heartbeat


def test_holder_defaults_safe():
    assert ts.TxAudioSettings().ceiling_dbfs == ts.CEILING_DEFAULT_DBFS


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"status": "ok", "tx_audio": {"ceiling_dbfs": -8.0}}, -8.0),
        ({"status": "ok", "tx_audio": {"ceiling_dbfs": 3.0}}, -3.0),
        ({"status": "ok", "tx_audio": {"ceiling_dbfs": "x"}}, -12.0),
        ({"status": "ok", "tx_audio": {"ceiling_dbfs": float("nan")}}, -12.0),
        ({"status": "ok", "tx_audio": None}, -12.0),
        ({"status": "ok"}, -12.0),  # old server: no key → safe default
        (None, -12.0),
        ("garbage", -12.0),
    ],
)
def test_update_from_heartbeat_is_total(body, expected):
    h = ts.TxAudioSettings()
    h.update_from_heartbeat(body)
    assert h.ceiling_dbfs == expected


def test_old_value_replaced_by_default_when_server_drops_it():
    h = ts.TxAudioSettings()
    h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -6.0}})
    h.update_from_heartbeat({"status": "ok"})
    assert h.ceiling_dbfs == -12.0


def test_thread_safe_reads():
    h = ts.TxAudioSettings()
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -6.0}})
            h.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -9.0}})

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(2000):
            assert h.ceiling_dbfs in (-6.0, -9.0, -12.0)
    finally:
        stop.set()
        t.join()


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Client:
    def __init__(self, resp):
        self.resp = resp

    def request(self, *a, **k):
        return self.resp


def test_send_heartbeat_feeds_holder(monkeypatch):
    monkeypatch.setattr("station_agent.heartbeat.collect_system_info", lambda config=None: {})
    h = ts.TxAudioSettings()
    ok = send_heartbeat(_Client(_Resp(200, {"tx_audio": {"ceiling_dbfs": -7.5}})), tx_settings=h)
    assert ok is True
    assert h.ceiling_dbfs == -7.5


def test_send_heartbeat_survives_non_json_body(monkeypatch):
    monkeypatch.setattr("station_agent.heartbeat.collect_system_info", lambda config=None: {})
    h = ts.TxAudioSettings()
    assert send_heartbeat(_Client(_Resp(200, ValueError("no json"))), tx_settings=h) is True
    assert h.ceiling_dbfs == -12.0


def test_agent_and_server_clamps_agree():
    from apps.stations import tx_audio

    for v in (None, -40, -24, -12.3, -3, 0, float("nan"), "x", True):
        assert ts.clamp_ceiling(v) == tx_audio.effective_ceiling_dbfs(v)
```

Plus a Django test in the same file for the server side — reuse the device-key heartbeat setup from `tests/test_heartbeat_variant.py` (copy its fixture/helper pattern for an authenticated POST):

```python
@pytest.mark.django_db
def test_heartbeat_response_carries_effective_ceiling(<auth fixtures from test_heartbeat_variant>):
    station.tx_audio_ceiling_dbfs = -9.0
    station.save(update_fields=["tx_audio_ceiling_dbfs"])
    resp = <authenticated POST /api/v1/heartbeat/ with a minimal valid payload>
    assert resp.status_code == 200
    assert resp.json()["tx_audio"] == {"ceiling_dbfs": -9.0, "calibrated": True}


@pytest.mark.django_db
def test_heartbeat_response_uncalibrated_default(...):
    resp = <same POST, field None>
    assert resp.json()["tx_audio"] == {"ceiling_dbfs": -12.0, "calibrated": False}
```

- [ ] **Step 2: Run — expect FAIL**

Run: `python -m pytest -q tests/test_heartbeat_tx_audio.py`

- [ ] **Step 3: Implement**

```python
# station_agent/audio/tx_settings.py
"""Thread-safe holder for server-pushed TX-audio calibration (spec §4).

Written by the heartbeat loop (main thread), read by the audio thread at each TX-bridge
start. The agent re-clamps every value — it never trusts the wire for an RF-safety bound.
"""

from __future__ import annotations

import math
import threading

CEILING_DEFAULT_DBFS = -12.0
CEILING_MIN_DBFS = -24.0
CEILING_MAX_DBFS = -3.0


def clamp_ceiling(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return CEILING_DEFAULT_DBFS
    if not math.isfinite(value):
        return CEILING_DEFAULT_DBFS
    return float(min(CEILING_MAX_DBFS, max(CEILING_MIN_DBFS, value)))


class TxAudioSettings:
    def __init__(self):
        self._lock = threading.Lock()
        self._ceiling = CEILING_DEFAULT_DBFS

    @property
    def ceiling_dbfs(self) -> float:
        with self._lock:
            return self._ceiling

    def update_from_heartbeat(self, body) -> None:
        raw = None
        if isinstance(body, dict):
            tx = body.get("tx_audio")
            if isinstance(tx, dict):
                raw = tx.get("ceiling_dbfs")
        value = clamp_ceiling(raw)
        with self._lock:
            self._ceiling = value
```

`send_heartbeat(http_client, config=None, tx_settings=None)`: on `status_code == 200`, if `tx_settings is not None`:
```python
        try:
            body = response.json()
        except ValueError:
            body = None
        tx_settings.update_from_heartbeat(body)
```
(`ValueError` covers `json.JSONDecodeError` and requests' `JSONDecodeError`.)

`HeartbeatView.post` final response:
```python
        from apps.stations.tx_audio import effective_ceiling_dbfs

        return Response(
            {
                "status": "ok",
                "tx_audio": {
                    "ceiling_dbfs": effective_ceiling_dbfs(station.tx_audio_ceiling_dbfs),
                    "calibrated": station.tx_audio_ceiling_dbfs is not None,
                },
            },
            status=status.HTTP_200_OK,
        )
```
(move the import to the module top.) `agent.py`: create `tx_settings = TxAudioSettings()` before the audio client block, pass `BridgeFactory(port_base=..., tx_settings=tx_settings)` (the kwarg is added in Task 4 — in this task add it to `BridgeFactory.__init__` as `tx_settings=None` stored on `self._tx_settings`, unused yet) and `send_heartbeat(http_client, config=config, tx_settings=tx_settings)`.

- [ ] **Step 4: Run** `python -m pytest -q tests/test_heartbeat_tx_audio.py tests/test_heartbeat_*.py tests/test_audio_bridge_factory.py` → PASS
- [ ] **Step 5: Commit** — `feat(agent): push TX ceiling calibration via heartbeat response`

---

### Task 3: Pure DSP policy + `build_tx_argv` DSP/meter fragment

**Files:**
- Create: `station_agent/audio/tx_dsp.py`
- Modify: `station_agent/audio/opus_bridge.py` (`build_tx_argv`)
- Test: `tests/test_audio_tx_dsp.py`

**Interfaces:**
- Consumes: `tx_settings.clamp_ceiling`.
- Produces:
  - `TxDspPolicy` (frozen dataclass): `hpf_hz=300`, `lpf_hz=3000`, `filter_poles=4`, `gate_threshold_dbfs=-45.0`, `gate_ratio=2.0`, `comp_threshold_dbfs=-30.0`, `comp_ratio=3.0`, `nominal_compressed_peak_dbfs=-24.0`, `headroom_db=3.0`, `limiter_ratio=1000.0`, `degraded_gain=1.0`.
  - `TxDspConfig` (frozen dataclass): `ceiling_dbfs: float`, `enabled: bool = True`, `policy: TxDspPolicy = TxDspPolicy()`; property `limiter_threshold -> float` (linear, `db_to_linear(ceiling)`); property `makeup_db -> float` (`ceiling - headroom - nominal_compressed_peak`).
  - `db_to_linear(db) -> float`
  - `DSP_ELEMENTS = ("audiocheblimit", "audiodynamic", "volume")`
  - `pre_limiter_fragment(cfg) -> list[str]` and `limiter_fragment(cfg) -> list[str]` — each a list of argv tokens **starting with** `"!"`; when `cfg.enabled` is False, `pre_limiter_fragment` returns `["!", "volume", f"volume={degraded_gain}"]` and `limiter_fragment` returns `[]`.
  - `build_tx_argv(tx_node, port, rate, *, dsp: TxDspConfig | None = None, meter: bool = False) -> list[str]` — `dsp=None, meter=False` returns **exactly** today's argv.

- [ ] **Step 1: Failing tests**

```python
# tests/test_audio_tx_dsp.py
import math

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
    order = [n for n in _pipeline_order(argv) if n in
             ("audiocheblimit", "audiodynamic", "volume", "pipewiresink")]
    assert order == ["audiocheblimit", "audiocheblimit", "audiodynamic", "audiodynamic",
                     "volume", "audiodynamic", "pipewiresink"]
    # stage modes in order: expander (gate), compressor, then hard-knee limiter
    dyn = [i for i, t in enumerate(argv) if t == "audiodynamic"]
    assert "mode=expander" in argv[dyn[0]:dyn[0] + 5]
    assert "mode=compressor" in argv[dyn[1]:dyn[1] + 5]
    lim = argv[dyn[2]:dyn[2] + 5]
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
    assert want in argv[dyn[2]:dyn[2] + 5]


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
```

- [ ] **Step 2: Run — expect FAIL** `python -m pytest -q tests/test_audio_tx_dsp.py`

- [ ] **Step 3: Implement**

```python
# station_agent/audio/tx_dsp.py
"""TX DSP policy → gst-launch argv fragments (spec 2026-10-03 §3.2, §4).

Chain (inserted between audioresample and pipewiresink, all in F32):
    band-pass (HPF+LPF) → gate (expander) → compressor → makeup (volume) → limiter

``audiodynamic`` is a memoryless per-sample curve (no attack/release/hang — verified with
gst-inspect). Hence: the gate is a low-threshold soft-knee expander that only touches
near-silence; the compressor is a static soft-knee "speech processor"; the limiter is a
hard-knee, very-high-ratio curve = a clipper at the ceiling. The SA818's LPF (ON by
default) band-limits the clip products. The limiter is ALWAYS the last stage so nothing
downstream re-introduces peaks (spec §5).

Degraded (any element missing): pass-through ``volume`` at ``degraded_gain`` (1.0 — no
makeup without a limiter; spec §4 fail-safe toward under-deviation).
"""

from __future__ import annotations

import dataclasses

from station_agent.audio.tx_settings import clamp_ceiling

DSP_ELEMENTS = ("audiocheblimit", "audiodynamic", "volume")


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


@dataclasses.dataclass(frozen=True)
class TxDspPolicy:
    hpf_hz: int = 300
    lpf_hz: int = 3000
    filter_poles: int = 4
    gate_threshold_dbfs: float = -45.0
    gate_ratio: float = 2.0
    comp_threshold_dbfs: float = -30.0
    comp_ratio: float = 3.0
    nominal_compressed_peak_dbfs: float = -24.0
    headroom_db: float = 3.0
    limiter_ratio: float = 1000.0
    degraded_gain: float = 1.0


@dataclasses.dataclass(frozen=True)
class TxDspConfig:
    ceiling_dbfs: float
    enabled: bool = True
    policy: TxDspPolicy = dataclasses.field(default_factory=TxDspPolicy)

    def __post_init__(self):
        # Defence in depth: whatever the caller passes, the ceiling is clamped here.
        object.__setattr__(self, "ceiling_dbfs", clamp_ceiling(self.ceiling_dbfs))

    @property
    def limiter_threshold(self) -> float:
        return db_to_linear(self.ceiling_dbfs)

    @property
    def makeup_db(self) -> float:
        p = self.policy
        return self.ceiling_dbfs - p.headroom_db - p.nominal_compressed_peak_dbfs


def pre_limiter_fragment(cfg: TxDspConfig) -> list[str]:
    p = cfg.policy
    if not cfg.enabled:
        return ["!", "volume", f"volume={p.degraded_gain}"]
    return [
        "!", "audiocheblimit", "mode=high-pass", f"cutoff={p.hpf_hz}", f"poles={p.filter_poles}",
        "!", "audiocheblimit", "mode=low-pass", f"cutoff={p.lpf_hz}", f"poles={p.filter_poles}",
        "!", "audiodynamic", "mode=expander", "characteristics=soft-knee",
        f"ratio={p.gate_ratio}", f"threshold={db_to_linear(p.gate_threshold_dbfs):.6f}",
        "!", "audiodynamic", "mode=compressor", "characteristics=soft-knee",
        f"ratio={p.comp_ratio}", f"threshold={db_to_linear(p.comp_threshold_dbfs):.6f}",
        "!", "volume", f"volume={db_to_linear(cfg.makeup_db):.6f}",
    ]


def limiter_fragment(cfg: TxDspConfig) -> list[str]:
    if not cfg.enabled:
        return []
    return [
        "!", "audiodynamic", "mode=compressor", "characteristics=hard-knee",
        f"ratio={cfg.policy.limiter_ratio}", f"threshold={cfg.limiter_threshold:.6f}",
    ]
```

`build_tx_argv(tx_node, port, rate, *, dsp=None, meter=False)`: keep head (udpsrc … `audioresample`) identical. Then:
- `dsp is None and not meter` → existing tail (`! audio/x-raw,rate={rate},channels=1 ! pipewiresink …`) unchanged.
- otherwise: `"!", f"audio/x-raw,format=F32LE,rate={rate},channels=1"` + `pre_limiter_fragment(dsp)` (if dsp) + (if meter: `"!", "tee", "name=txm", "!", "queue"`) + `limiter_fragment(dsp)` (if dsp) + `"!", "audioconvert", "!", "pipewiresink", f"target-object={tx_node}", "sync=false"` + (if meter: `"txm.", "!", "queue", "leaky=downstream", "max-size-buffers=8", "!", "fdsink", "fd=1", "sync=false"`).
Update the module docstring's TX line to mention the DSP fragment and meter tap.

- [ ] **Step 4: Validate the real argv once with gst (skip-if-no-gst test)**

Add to the test file:
```python
import shutil, subprocess
import pytest

@pytest.mark.skipif(shutil.which("gst-launch-1.0") is None, reason="no gst")
def test_real_gst_accepts_dsp_fragment():
    for el in tx_dsp.DSP_ELEMENTS:
        if subprocess.run(["gst-inspect-1.0", "--exists", el]).returncode != 0:
            pytest.skip(f"{el} not installed")
    cfg = _cfg()
    argv = ["gst-launch-1.0", "-q", "audiotestsrc", "num-buffers=20", "!", "audioconvert",
            "!", "audioresample", "!", "audio/x-raw,format=F32LE,rate=16000,channels=1",
            *tx_dsp.pre_limiter_fragment(cfg), *tx_dsp.limiter_fragment(cfg),
            "!", "audioconvert", "!", "fakesink"]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert "WARN" not in r.stderr.upper() and "WARNUNG" not in r.stderr.upper()
```
Run: `python -m pytest -q tests/test_audio_tx_dsp.py tests/test_audio_opus_bridge.py` → PASS
- [ ] **Step 5: Commit** — `feat(agent): TX DSP chain policy + argv builder (band-pass/gate/comp/makeup/limiter)`

---

### Task 4: TX meter math + TxBridge DSP/degradation/meter reader + factory wiring

**Files:**
- Create: `station_agent/audio/tx_meter.py`
- Modify: `station_agent/audio/opus_bridge.py` (`TxBridge`), `station_agent/audio/bridge_factory.py` (`make_tx`, `_PortBoundTx`)
- Test: `tests/test_audio_tx_meter.py`, `tests/test_audio_tx_bridge_dsp.py`

**Interfaces:**
- Consumes: `TxDspConfig`, `DSP_ELEMENTS`, `build_tx_argv(..., dsp=, meter=)`, `TxAudioSettings.ceiling_dbfs`.
- Produces:
  - `tx_meter.METER_HZ = 8`; `tx_meter.chunk_bytes(rate) -> int` (= `rate // METER_HZ * 4`);
  - `tx_meter.compute_meter(pcm_f32: bytes, *, limiter_threshold: float | None, limiter_ratio: float) -> dict` → `{"peak_dbfs": float|None, "rms_dbfs": float|None, "gain_reduction_db": float, "limiting": bool}` (values rounded to 1 decimal; `None` = silence; `limiting = gain_reduction_db >= 0.5`).
  - `opus_bridge.probe_dsp_available(inspect=None) -> bool` (cached per process; `inspect(name) -> bool`, default runs `gst-inspect-1.0 --exists`; any OSError → False).
  - `TxBridge(tx_node, port, rate, *, spawn=None, socket_factory=_udp_socket, ssrc=..., dsp: TxDspConfig | None = None, on_meter=None, start_reader: bool = True, startup_grace: float = 0.3)`; attribute `dsp_mode: str` in `{"off", "full", "degraded"}`.
  - `on_meter(reading: dict)` is called from the reader thread with `compute_meter(...)` output plus `"dsp": dsp_mode` and `"ceiling_dbfs": cfg.ceiling_dbfs`.
  - `BridgeFactory(port_base=47000, tx_settings=None, dsp_probe=None)`; `make_tx(node, rate, on_meter=None)`.

- [ ] **Step 1: Failing meter-math tests**

```python
# tests/test_audio_tx_meter.py
import math
import struct

from station_agent.audio import tx_meter


def _f32(samples):
    return struct.pack(f"<{len(samples)}f", *samples)


def test_chunk_bytes():
    assert tx_meter.chunk_bytes(16000) == 2000 * 4


def test_silence_is_none_and_no_limiting():
    r = tx_meter.compute_meter(_f32([0.0] * 100), limiter_threshold=0.25, limiter_ratio=1000.0)
    assert r == {"peak_dbfs": None, "rms_dbfs": None, "gain_reduction_db": 0.0, "limiting": False}


def test_below_ceiling_passes_untouched():
    r = tx_meter.compute_meter(_f32([0.1, -0.1] * 50), limiter_threshold=0.25, limiter_ratio=1000.0)
    assert r["peak_dbfs"] == -20.0
    assert r["gain_reduction_db"] == 0.0 and r["limiting"] is False


def test_over_ceiling_is_held_at_ceiling_with_gain_reduction():
    r = tx_meter.compute_meter(_f32([1.0, -1.0] * 50), limiter_threshold=0.25, limiter_ratio=1000.0)
    assert r["peak_dbfs"] == round(20 * math.log10(0.25 + 0.75 / 1000), 1)  # ≈ -12.0
    assert r["gain_reduction_db"] == round(20 * math.log10(1.0 / (0.25 + 0.75 / 1000)), 1)
    assert r["limiting"] is True


def test_no_limiter_models_s16_clip_at_full_scale():
    r = tx_meter.compute_meter(_f32([2.0] * 10), limiter_threshold=None, limiter_ratio=1.0)
    assert r["peak_dbfs"] == 0.0
    assert r["limiting"] is False  # no limiter in path; clip is reported via peak 0 dBFS


def test_nan_inf_samples_ignored():
    r = tx_meter.compute_meter(_f32([float("nan"), float("inf"), 0.1]), limiter_threshold=0.25,
                               limiter_ratio=1000.0)
    assert r["peak_dbfs"] == -20.0


def test_partial_trailing_bytes_ignored():
    r = tx_meter.compute_meter(_f32([0.1]) + b"\x00\x01", limiter_threshold=0.25, limiter_ratio=1000.0)
    assert r["peak_dbfs"] == -20.0
```

- [ ] **Step 2: Implement `tx_meter.py`**

```python
# station_agent/audio/tx_meter.py
"""TX modulation meter (spec §3.5).

The meter taps the chain right BEFORE the limiter (F32, so nothing has clipped yet) and
runs the samples through the limiter's exact static curve. ``audiodynamic`` is memoryless,
so this reproduces the post-limiter samples (the level at D — the sink is unity since
linux-image #99) AND yields the true limiter gain reduction from one tap. Final F32→S16
conversion clips at full scale; modelled as ``min(|y|, 1.0)``.
"""

from __future__ import annotations

import math
import struct

METER_HZ = 8
_LIMITING_DB = 0.5


def chunk_bytes(rate: int) -> int:
    return (rate // METER_HZ) * 4


def _db(x: float) -> float | None:
    return round(20.0 * math.log10(x), 1) if x > 0 else None


def compute_meter(pcm_f32: bytes, *, limiter_threshold: float | None, limiter_ratio: float) -> dict:
    n = len(pcm_f32) // 4
    pre_peak = post_peak = 0.0
    acc = 0.0
    count = 0
    t = limiter_threshold
    for (x,) in struct.iter_unpack("<f", pcm_f32[: n * 4]):
        if not math.isfinite(x):
            continue
        a = abs(x)
        y = a if (t is None or a <= t) else t + (a - t) / limiter_ratio
        y = min(y, 1.0)
        pre_peak = max(pre_peak, a)
        post_peak = max(post_peak, y)
        acc += y * y
        count += 1
    if post_peak <= 0.0:
        return {"peak_dbfs": None, "rms_dbfs": None, "gain_reduction_db": 0.0, "limiting": False}
    gr = 0.0
    if t is not None and pre_peak > t:
        gr = round(20.0 * math.log10(min(pre_peak, 1e6) / post_peak), 1)
    return {
        "peak_dbfs": _db(post_peak),
        "rms_dbfs": _db(math.sqrt(acc / count)),
        "gain_reduction_db": max(0.0, gr),
        "limiting": gr >= _LIMITING_DB,
    }
```
Note `test_no_limiter_models_s16_clip_at_full_scale`: with `t=None` gr stays 0 → `limiting False`. Good.

Run `python -m pytest -q tests/test_audio_tx_meter.py` → PASS.

- [ ] **Step 3: Failing bridge tests**

```python
# tests/test_audio_tx_bridge_dsp.py
import io
import struct
import subprocess

from station_agent.audio import opus_bridge, tx_dsp
from station_agent.audio.bridge_factory import BridgeFactory
from station_agent.audio.tx_settings import TxAudioSettings


class FakeProc:
    def __init__(self, stdout=None, exits_early=False):
        self.stdout = stdout
        self._early = exits_early
        self.terminated = False

    def poll(self):
        return 1 if (self._early or self.terminated) else None

    def wait(self, timeout=None):
        if self._early or self.terminated:
            return 1
        raise subprocess.TimeoutExpired("gst", timeout)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


class FakeSock:
    def sendto(self, *a):
        pass

    def close(self):
        pass


def _spawner(procs):
    calls = []

    def spawn(argv):
        calls.append(argv)
        return procs.pop(0)

    return spawn, calls


def test_no_dsp_no_meter_is_legacy_argv():
    spawn, calls = _spawner([FakeProc()])
    b = opus_bridge.TxBridge("n", 47000, 16000, spawn=spawn, socket_factory=FakeSock)
    b.start()
    assert calls[0] == opus_bridge.build_tx_argv("n", 47000, 16000)
    assert b.dsp_mode == "off"
    b.stop()


def test_full_dsp_spawned_and_mode_full():
    spawn, calls = _spawner([FakeProc()])
    cfg = tx_dsp.TxDspConfig(ceiling_dbfs=-12.0)
    b = opus_bridge.TxBridge("n", 47000, 16000, spawn=spawn, socket_factory=FakeSock, dsp=cfg)
    b.start()
    assert "audiodynamic" in calls[0]
    assert b.dsp_mode == "full"
    b.stop()


def test_dsp_pipeline_dying_at_startup_falls_back_to_degraded():
    spawn, calls = _spawner([FakeProc(exits_early=True), FakeProc()])
    cfg = tx_dsp.TxDspConfig(ceiling_dbfs=-12.0)
    b = opus_bridge.TxBridge("n", 47000, 16000, spawn=spawn, socket_factory=FakeSock, dsp=cfg)
    b.start()
    assert len(calls) == 2
    assert "audiodynamic" not in calls[1] and "volume=1.0" in calls[1]
    assert b.dsp_mode == "degraded"
    b.stop()


def test_disabled_config_is_degraded_without_retry():
    spawn, calls = _spawner([FakeProc()])
    cfg = tx_dsp.TxDspConfig(ceiling_dbfs=-12.0, enabled=False)
    b = opus_bridge.TxBridge("n", 47000, 16000, spawn=spawn, socket_factory=FakeSock, dsp=cfg)
    b.start()
    assert len(calls) == 1 and b.dsp_mode == "degraded"
    b.stop()


def test_meter_reader_emits_readings_from_stdout():
    chunk = struct.pack("<2000f", *([1.0, -1.0] * 1000))
    proc = FakeProc(stdout=io.BytesIO(chunk * 2))
    spawn, _ = _spawner([proc])
    got = []
    cfg = tx_dsp.TxDspConfig(ceiling_dbfs=-12.0)
    b = opus_bridge.TxBridge("n", 47000, 16000, spawn=spawn, socket_factory=FakeSock,
                             dsp=cfg, on_meter=got.append)
    b.start()
    b.stop()  # joins the reader; BytesIO EOF ends it
    assert len(got) == 2
    assert got[0]["limiting"] is True
    assert got[0]["dsp"] == "full" and got[0]["ceiling_dbfs"] == -12.0


def test_meter_callback_exception_does_not_kill_reader():
    chunk = struct.pack("<2000f", *([0.1] * 2000))
    proc = FakeProc(stdout=io.BytesIO(chunk * 3))
    spawn, _ = _spawner([proc])
    seen = []

    def boom(r):
        seen.append(r)
        raise RuntimeError("consumer bug")

    b = opus_bridge.TxBridge("n", 47000, 16000, spawn=spawn, socket_factory=FakeSock,
                             dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0), on_meter=boom)
    b.start()
    b.stop()
    assert len(seen) == 3


def test_probe_dsp_available_uses_inspect_and_handles_oserror():
    opus_bridge.probe_dsp_available.cache_clear()
    assert opus_bridge.probe_dsp_available(inspect=lambda n: True) is True
    opus_bridge.probe_dsp_available.cache_clear()
    assert opus_bridge.probe_dsp_available(inspect=lambda n: n != "audiodynamic") is False
    opus_bridge.probe_dsp_available.cache_clear()

    def raising(n):
        raise OSError("no gst-inspect")

    assert opus_bridge.probe_dsp_available(inspect=raising) is False
    opus_bridge.probe_dsp_available.cache_clear()


def test_factory_make_tx_uses_current_ceiling_and_probe():
    s = TxAudioSettings()
    s.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -8.0}})
    f = BridgeFactory(port_base=47100, tx_settings=s, dsp_probe=lambda: False)
    b = f.make_tx("n", 16000, on_meter=lambda r: None)
    assert b._dsp.ceiling_dbfs == -8.0 and b._dsp.enabled is False
    b2 = BridgeFactory(port_base=47200).make_tx("n", 16000)  # no settings → safe default
    assert b2._dsp.ceiling_dbfs == -12.0
```

`probe_dsp_available` takes a non-hashable-free arg (a function) so `functools.lru_cache` works (functions are hashable). The default `inspect=None` → `_gst_inspect_exists`.

- [ ] **Step 4: Implement TxBridge changes**

In `opus_bridge.py`:
```python
import functools
from station_agent.audio import tx_meter
from station_agent.audio.tx_dsp import DSP_ELEMENTS, TxDspConfig
import dataclasses


def _spawn_with_stdout(argv: list[str]):
    # Meter tap: PCM on stdout. stderr stays DEVNULL (gst -q only prints errors).
    return subprocess.Popen(  # noqa: S603 — argv is fixed tool + resolved node/port
        argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )


def _gst_inspect_exists(name: str) -> bool:
    return (
        subprocess.run(  # noqa: S603,S607 — fixed tool name, element name from a constant
            ["gst-inspect-1.0", "--exists", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        ).returncode == 0
    )


@functools.lru_cache(maxsize=4)
def probe_dsp_available(inspect=None) -> bool:
    """True iff every DSP element exists. Cached: the image does not change at runtime."""
    check = inspect or _gst_inspect_exists
    try:
        return all(check(el) for el in DSP_ELEMENTS)
    except (OSError, subprocess.SubprocessError):
        return False
```

`TxBridge.__init__` gains `dsp=None, on_meter=None, start_reader=True, startup_grace=0.3`, `spawn=None`. Store; `self.dsp_mode = "off"`; `self._reader=None`. `start()`:
```python
    def start(self) -> None:
        self._sock = self._socket_factory()
        meter = self._on_meter is not None
        spawn = self._spawn or (_spawn_with_stdout if meter else _default_spawn)
        cfg = self._dsp
        self._proc = spawn(build_tx_argv(self._node, self._port, self._rate, dsp=cfg, meter=meter))
        if cfg is None:
            self.dsp_mode = "off"
        elif not cfg.enabled:
            self.dsp_mode = "degraded"
        elif self._died_at_startup(self._proc):
            # Element present but the pipeline failed to construct/link: TX must never
            # break entirely (spec §3.2) → plain pass-through, surfaced via the meter.
            logger.warning("tx-bridge: DSP pipeline exited at startup — falling back to pass-through")
            self._dsp = cfg = dataclasses.replace(cfg, enabled=False)
            self._proc = spawn(build_tx_argv(self._node, self._port, self._rate, dsp=cfg, meter=meter))
            self.dsp_mode = "degraded"
        else:
            self.dsp_mode = "full"
        if meter and self._start_reader and getattr(self._proc, "stdout", None) is not None:
            self._reader = threading.Thread(target=self._meter_loop, name=f"tx-meter-{self._port}", daemon=True)
            self._reader.start()

    def _died_at_startup(self, proc) -> bool:
        try:
            proc.wait(timeout=self._startup_grace)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _meter_loop(self) -> None:
        stream = self._proc.stdout
        size = tx_meter.chunk_bytes(self._rate)
        cfg = self._dsp
        threshold = cfg.limiter_threshold if (cfg is not None and cfg.enabled) else None
        ratio = cfg.policy.limiter_ratio if cfg is not None else 1.0
        ceiling = cfg.ceiling_dbfs if cfg is not None else None
        while True:
            try:
                buf = _read_exact(stream, size)
            except (OSError, ValueError):
                return  # pipe closed on stop()
            if not buf:
                return
            reading = tx_meter.compute_meter(buf, limiter_threshold=threshold, limiter_ratio=ratio)
            reading["dsp"] = self.dsp_mode
            reading["ceiling_dbfs"] = ceiling
            try:
                self._on_meter(reading)
            except Exception:  # noqa: BLE001 — a consumer error must not kill the meter
                logger.exception("tx-bridge: on_meter callback raised")
```
with
```python
def _read_exact(stream, size: int) -> bytes:
    """Read up to ``size`` bytes; returns b"" only at EOF (a short final chunk is dropped)."""
    chunks, got = [], 0
    while got < size:
        b = stream.read(size - got)
        if not b:
            return b""
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)
```
`stop()`: after `_terminate(self._proc)`, close `proc.stdout` if present (ignore OSError), then join `self._reader` (timeout `_STOP_WAIT`).
Note the dead-at-startup check costs ≤0.3 s once per TX start — `bridge.start` already runs off-loop in a thread (`engine._to_thread`), so the WS loop is not stalled; a fake proc in tests returns instantly.
Keep the existing `TxBridge` tests passing: the existing tests pass `spawn=` explicitly; their fake proc must support `wait(timeout)` only when `dsp` is set — with `dsp=None` `_died_at_startup` is not called.

`bridge_factory.py`:
```python
from station_agent.audio.opus_bridge import PortAllocator, RxBridge, TxBridge, probe_dsp_available
from station_agent.audio.tx_dsp import TxDspConfig
from station_agent.audio.tx_settings import CEILING_DEFAULT_DBFS

class BridgeFactory:
    def __init__(self, port_base: int = 47000, tx_settings=None, dsp_probe=None):
        self._ports = PortAllocator(base=port_base)
        self._tx_settings = tx_settings
        self._dsp_probe = dsp_probe or probe_dsp_available

    def make_tx(self, node: str, rate: int, on_meter=None):
        port = self._ports.acquire()
        ceiling = self._tx_settings.ceiling_dbfs if self._tx_settings else CEILING_DEFAULT_DBFS
        dsp = TxDspConfig(ceiling_dbfs=ceiling, enabled=self._dsp_probe())
        return _PortBoundTx(node, port, rate, self._ports, dsp=dsp, on_meter=on_meter)
```
`_PortBoundTx.__init__(self, node, port, rate, ports, *, dsp=None, on_meter=None)` → `super().__init__(node, port, rate, dsp=dsp, on_meter=on_meter)`.

- [ ] **Step 5: Run** `python -m pytest -q tests/test_audio_tx_meter.py tests/test_audio_tx_bridge_dsp.py tests/test_audio_opus_bridge.py tests/test_audio_bridge_factory.py` → PASS
- [ ] **Step 6: Commit** — `feat(agent): TX bridge runs DSP chain with graceful degradation + pre-limiter meter tap`

---

### Task 5: Engine emits `tx_meter`; U-diagnostic measures with the DSP in path

**Files:**
- Modify: `station_agent/audio/engine.py` (`on_mic_state`, `_teardown_tx`), `station_agent/audio/diagnostics.py` (`build_measured_tx_argv` + `MeasuredTxBridge` accept `dsp`), `station_agent/audio/bridge_factory.py` (`make_diag_u` passes the same `TxDspConfig`)
- Modify (fakes accept new kwarg): `tests/test_audio_engine.py:60`, `tests/test_audio_e2e.py:118`, `tests/test_audio_ws_client.py:91`, `tests/test_audio_diag_engine.py:501` → `def make_tx(self, node, rate, on_meter=None)`
- Test: `tests/test_audio_engine_tx_meter.py`, extend `tests/test_audio_diag_measured_bridge.py`

**Interfaces:**
- Consumes: `make_tx(node, rate, on_meter=...)`, meter reading dict (Task 4).
- Produces: agent→server JSON `{"v": 1, "type": "tx_meter", "slot": int, "active": bool, "peak_dbfs": float|None, "rms_dbfs": float|None, "gain_reduction_db": float, "limiting": bool, "dsp": "off"|"full"|"degraded", "ceiling_dbfs": float|None}`. On teardown one final frame `{"v":1,"type":"tx_meter","slot":<slot>,"active":false}`. `build_measured_tx_argv(tx_node, port, rate, *, meas_fd=MEAS_FD, dsp=None)`.

- [ ] **Step 1: Failing engine tests** — build on `make_engine()` from `tests/test_audio_engine.py` (import it: `from tests.test_audio_engine import make_engine, FakeTx` if `tests` is a package; otherwise copy the minimal fakes). The fake factory records `on_meter`:

```python
# tests/test_audio_engine_tx_meter.py
import asyncio

import pytest

from tests.test_audio_engine import make_engine  # adapt if tests/ is not a package


@pytest.mark.asyncio
async def test_meter_reading_from_bridge_thread_is_emitted_as_tx_meter():
    engine, factory, sent_json, _ = make_engine()
    await engine.start()
    await engine.on_mic_state(active=True, tx_slot=1, tx_module="fm")
    on_meter = factory.last_tx_on_meter
    assert on_meter is not None
    reading = {"peak_dbfs": -12.0, "rms_dbfs": -18.0, "gain_reduction_db": 3.1,
               "limiting": True, "dsp": "full", "ceiling_dbfs": -12.0}
    # called from a foreign thread, like the real reader
    await asyncio.get_running_loop().run_in_executor(None, on_meter, reading)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    meters = [m for m in sent_json if m.get("type") == "tx_meter"]
    assert meters[-1] == {"v": 1, "type": "tx_meter", "slot": 1, "active": True, **reading}


@pytest.mark.asyncio
async def test_teardown_emits_inactive_meter_and_late_readings_dropped():
    engine, factory, sent_json, _ = make_engine()
    await engine.start()
    await engine.on_mic_state(active=True, tx_slot=1, tx_module="fm")
    on_meter = factory.last_tx_on_meter
    await engine.on_mic_state(active=False, tx_slot=None, tx_module=None)
    assert {"v": 1, "type": "tx_meter", "slot": 1, "active": False} in sent_json
    n = len(sent_json)
    on_meter({"peak_dbfs": -1.0, "rms_dbfs": -2.0, "gain_reduction_db": 0.0,
              "limiting": False, "dsp": "full", "ceiling_dbfs": -12.0})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(sent_json) == n  # a late reading from a torn-down bridge is dropped
```
(Update `FakeFactory.make_tx` in `tests/test_audio_engine.py` to `def make_tx(self, node, rate, on_meter=None): self.last_tx_on_meter = on_meter; ...` and initialise `self.last_tx_on_meter = None`. Check the file uses `pytest.mark.asyncio` or `asyncio.run` and match it.)

- [ ] **Step 2: Implement engine**

In `on_mic_state` before `make_tx`:
```python
        loop = asyncio.get_running_loop()
        token = object()
        self._tx_meter_token = token
        slot_for_meter = tx_slot

        def on_meter(reading: dict, _token=token) -> None:
            # Reader thread → loop. The token drops readings from a superseded/torn-down
            # bridge (the reader may emit one last chunk while stop() joins it).
            loop.call_soon_threadsafe(self._emit_tx_meter, _token, slot_for_meter, reading)

        bridge = self._factory.make_tx(node, mic_info.rate, on_meter=on_meter)
```
```python
    def _emit_tx_meter(self, token, slot, reading: dict) -> None:
        if token is not self._tx_meter_token:
            return
        msg = {"v": 1, "type": "tx_meter", "slot": slot, "active": True, **reading}
        asyncio.ensure_future(self._emit_json(msg))
```
`__init__`: `self._tx_meter_token: object | None = None`. `_teardown_tx`: if `self._tx is not None`: capture `slot = self._tx["slot"]`, set `self._tx_meter_token = None` **before** stopping the bridge, stop it, then `await self._emit_json({"v": 1, "type": "tx_meter", "slot": slot, "active": False})`. Also clear the token on a failed bridge start.

- [ ] **Step 3: U-diagnostic with DSP** — add `dsp=None` to `build_measured_tx_argv`; when given, insert `"!", f"audio/x-raw,format=F32LE,rate={rate},channels=1", *pre_limiter_fragment(dsp), *limiter_fragment(dsp)` after the `audio/x-raw,rate=…` caps and before `tee name=t` (so the measurement tap sees the post-limiter = D signal — this is the "harness with DSP in path" of spec §8). `MeasuredTxBridge(..., dsp=None)` stores and forwards it; `BridgeFactory.make_diag_u` builds the same `TxDspConfig` as `make_tx`. Test in `tests/test_audio_diag_measured_bridge.py`:

```python
def test_measured_tx_argv_with_dsp_measures_post_limiter():
    from station_agent.audio import tx_dsp
    from station_agent.audio.diagnostics import build_measured_tx_argv

    argv = build_measured_tx_argv("n", 47000, 16000, dsp=tx_dsp.TxDspConfig(ceiling_dbfs=-12.0))
    dyn = [i for i, t in enumerate(argv) if t == "audiodynamic"]
    assert len(dyn) == 3 and dyn[-1] < argv.index("tee")


def test_measured_tx_argv_without_dsp_unchanged():
    from station_agent.audio.diagnostics import build_measured_tx_argv

    assert "audiodynamic" not in build_measured_tx_argv("n", 47000, 16000)
```

- [ ] **Step 4: Run** `python -m pytest -q tests/test_audio_*.py` → PASS
- [ ] **Step 5: Commit** — `feat(agent): emit tx_meter over audio WS; U diagnostic measures with DSP in path`

---

### Task 6: Server relays `tx_meter` to browsers (sanitized)

**Files:**
- Create: `apps/audio/tx_meter.py`
- Modify: `apps/audio/consumers.py` (`AgentAudioConsumer.receive` ~line 136; `AudioConsumer` add `audio_tx_meter` near line 721)
- Test: `tests/test_audio_tx_meter_relay.py`

**Interfaces:**
- Consumes: agent `tx_meter` frame (Task 5).
- Produces: `apps.audio.tx_meter.sanitize(msg) -> dict | None`; browser receives JSON `{"v": 1, "type": "tx_meter", "slot", "active", "peak_dbfs", "rms_dbfs", "gain_reduction_db", "limiting", "dsp", "ceiling_dbfs"}` (inactive frames carry only `v,type,slot,active`).

- [ ] **Step 1: Failing tests**

```python
# tests/test_audio_tx_meter_relay.py
import math

import pytest

from apps.audio.tx_meter import sanitize

GOOD = {"v": 1, "type": "tx_meter", "slot": 1, "active": True, "peak_dbfs": -12.0,
        "rms_dbfs": -18.2, "gain_reduction_db": 3.0, "limiting": True, "dsp": "full",
        "ceiling_dbfs": -12.0}


def test_good_frame_passes_through():
    assert sanitize(GOOD) == GOOD


def test_inactive_frame_minimal():
    assert sanitize({"type": "tx_meter", "slot": 2, "active": False}) == {
        "v": 1, "type": "tx_meter", "slot": 2, "active": False}


@pytest.mark.parametrize("field,bad", [
    ("peak_dbfs", float("nan")), ("peak_dbfs", 1e9), ("peak_dbfs", "loud"),
    ("rms_dbfs", float("inf")), ("gain_reduction_db", -5.0), ("gain_reduction_db", 1e9),
    ("ceiling_dbfs", "x"),
])
def test_bad_numbers_become_none_or_clamped(field, bad):
    out = sanitize({**GOOD, field: bad})
    v = out[field]
    assert v is None or (isinstance(v, float) and math.isfinite(v) and -120.0 <= v <= 120.0)
    if field == "gain_reduction_db":
        assert 0.0 <= v <= 60.0


def test_unknown_dsp_and_extra_keys_dropped():
    out = sanitize({**GOOD, "dsp": "<script>", "evil": 1})
    assert out["dsp"] == "off" and "evil" not in out


@pytest.mark.parametrize("bad", [None, [], "x", {"type": "tx_meter"}, {**GOOD, "slot": "1"},
                                 {**GOOD, "slot": True}])
def test_unroutable_frames_rejected(bad):
    assert sanitize(bad) is None


def test_limiting_coerced_to_bool():
    assert sanitize({**GOOD, "limiting": 1})["limiting"] is True
```

Plus a Channels test: copy the agent+browser communicator setup from `tests/test_audio_consumer.py` (agent connects with Ed25519 query auth, browser user connects), agent sends `GOOD` as text → browser receives exactly `GOOD`; agent sends `{**GOOD, "slot": "x"}` → browser receives nothing within 0.2 s (`await browser.receive_nothing(timeout=0.2)`; first drain any connect-time frames the browser normally gets — follow the existing test's pattern).

- [ ] **Step 2: Implement**

```python
# apps/audio/tx_meter.py
"""Sanitize agent → browser TX modulation-meter frames (spec §3.5).

The agent is semi-trusted (Ed25519-authenticated device, but its frames reach every
operator's browser): whitelist keys, coerce types, bound numbers.
"""

import math

_DSP_MODES = {"off", "full", "degraded"}


def _num(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(min(hi, max(lo, v)))


def sanitize(msg):
    if not isinstance(msg, dict):
        return None
    slot = msg.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int):
        return None
    if "active" not in msg:
        return None
    out = {"v": 1, "type": "tx_meter", "slot": slot, "active": bool(msg.get("active"))}
    if not out["active"]:
        return out
    gr = _num(msg.get("gain_reduction_db"), 0.0, 60.0)
    dsp = msg.get("dsp")
    out.update(
        {
            "peak_dbfs": _num(msg.get("peak_dbfs"), -120.0, 0.0),
            "rms_dbfs": _num(msg.get("rms_dbfs"), -120.0, 0.0),
            "gain_reduction_db": 0.0 if gr is None else gr,
            "limiting": bool(msg.get("limiting")),
            "dsp": dsp if dsp in _DSP_MODES else "off",
            "ceiling_dbfs": _num(msg.get("ceiling_dbfs"), -120.0, 0.0),
        }
    )
    return out
```
Adjust `test_bad_numbers_become_none_or_clamped` expectations to these bounds (dBFS clamp to `[-120, 0]`, so `1e9 → 0.0`; GR `-5 → 0.0`, `1e9 → 60.0`) — the parametrized assertion already allows this.

`AgentAudioConsumer.receive`: 
```python
        elif mtype == "tx_meter":
            clean = tx_meter.sanitize(msg)
            if clean is not None:
                await self.channel_layer.group_send(
                    self.browser_group, {"type": "audio.tx_meter", "msg": clean}
                )
```
`AudioConsumer`:
```python
    async def audio_tx_meter(self, event):
        await self.send(text_data=json.dumps(event["msg"]))
```
(Mirror how `audio_stream_state` sends — use the same send helper if there is one.)

- [ ] **Step 3: Run** `python -m pytest -q tests/test_audio_tx_meter_relay.py tests/test_audio_consumer.py` → PASS
- [ ] **Step 4: Commit** — `feat(audio): relay sanitized tx_meter frames to browsers`

---

### Task 7: Capability `write_role` policy map + viewer role

**Files:**
- Create: `apps/control/capability_policy.py`
- Test: `tests/test_control_capability_policy.py`

**Interfaces:**
- Produces:
  - `ROLE_RANK = {"operator": 1, "station_manager": 2, "staff": 3, "admin": 4}`
  - `CapabilityPolicy` (frozen dataclass): `write_role: str = "operator"`, `persist: bool = False`
  - `POLICY: dict[tuple[str, str | None], CapabilityPolicy]` — keys `(capability_name, module_type_or_None)`; entries `("filter_pre_emphasis", None)`, `("filter_hpf", None)`, `("filter_lpf", None)` → `CapabilityPolicy("staff", persist=True)`.
  - `policy_for(capability: str, module_type: str | None) -> CapabilityPolicy` (exact `(cap, type)` first, then `(cap, None)`, else default operator/no-persist).
  - `viewer_role(user, station) -> str | None` (`None` if the user may not use the station at all): admin if `user.is_admin`; staff if `user.is_internal`; station_manager if `user.can_administer_station(station)`; operator if `user.can_use_station(station)`.
  - `can_write(user, station, capability, module_type) -> bool`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_control_capability_policy.py
import pytest

from apps.control import capability_policy as cp


def test_unknown_caps_default_operator_no_persist():
    p = cp.policy_for("frequency", "fm")
    assert p.write_role == "operator" and p.persist is False


@pytest.mark.parametrize("cap", ["filter_pre_emphasis", "filter_hpf", "filter_lpf"])
def test_filters_are_staff_and_persisted(cap):
    p = cp.policy_for(cap, "fm")
    assert p.write_role == "staff" and p.persist is True


def test_exact_module_type_entry_wins(monkeypatch):
    monkeypatch.setitem(cp.POLICY, ("power", "fm"), cp.CapabilityPolicy("admin"))
    assert cp.policy_for("power", "fm").write_role == "admin"
    assert cp.policy_for("power", "hf").write_role == "operator"


def test_every_policy_role_is_ranked():
    for p in cp.POLICY.values():
        assert p.write_role in cp.ROLE_RANK


@pytest.mark.django_db
@pytest.mark.parametrize(
    "who,expected_role,may_write_filter",
    [
        ("applicant", None, False),
        ("member", "operator", False),
        ("region_manager", "station_manager", False),
        ("station_admin", "station_manager", False),
        ("staff", "staff", True),
        ("admin", "admin", True),
    ],
)
def test_viewer_role_matrix(who, expected_role, may_write_filter, <fixtures that build a station + a user of each kind>):
    user = <user for `who`>
    assert cp.viewer_role(user, station) == expected_role
    assert cp.can_write(user, station, "filter_hpf", "fm") is may_write_filter
    assert cp.can_write(user, station, "frequency", "fm") is (expected_role is not None)
```
Build the users with the existing helpers (grep `tests/conftest.py` and `tests/test_control_consumer_lock.py` for how a region manager / station admin / staff user is created — `RegionAssignment.objects.create(user=..., region=..., role="manager")`, `StationAssignment.objects.create(user=..., station=..., role="admin")`, `membership_level=...`). Write a small local helper `_user(kind, station)` in the test file.

- [ ] **Step 2: Implement**

```python
# apps/control/capability_policy.py
"""Platform-layer capability write policy (spec 2026-10-03 §4a).

Firmware ``describe`` is role-agnostic; the server decides who may change what. A capability
without an entry keeps today's behaviour (any lock-holding operator). ``persist`` marks
calibration-type values the server stores per station and re-applies through the agent.
Per-station overrides are a later extension (YAGNI).
"""

from __future__ import annotations

import dataclasses

ROLE_RANK = {"operator": 1, "station_manager": 2, "staff": 3, "admin": 4}


@dataclasses.dataclass(frozen=True)
class CapabilityPolicy:
    write_role: str = "operator"
    persist: bool = False


_DEFAULT = CapabilityPolicy()
_STAFF_PERSISTED = CapabilityPolicy(write_role="staff", persist=True)

POLICY: dict[tuple[str, str | None], CapabilityPolicy] = {
    # SA818 filters (FW FilterCap, FW-RemoteStation #82): calibration, not a per-QSO control.
    ("filter_pre_emphasis", None): _STAFF_PERSISTED,
    ("filter_hpf", None): _STAFF_PERSISTED,
    ("filter_lpf", None): _STAFF_PERSISTED,
}


def policy_for(capability: str, module_type: str | None) -> CapabilityPolicy:
    return POLICY.get((capability, module_type)) or POLICY.get((capability, None)) or _DEFAULT


def viewer_role(user, station) -> str | None:
    if user is None or getattr(user, "is_anonymous", True):
        return None
    if user.is_admin:
        return "admin"
    if user.is_internal:
        return "staff"
    if user.can_administer_station(station):
        return "station_manager"
    if user.can_use_station(station):
        return "operator"
    return None


def can_write(user, station, capability: str, module_type: str | None) -> bool:
    role = viewer_role(user, station)
    if role is None:
        return False
    return ROLE_RANK[role] >= ROLE_RANK[policy_for(capability, module_type).write_role]
```

- [ ] **Step 3: Run** `python -m pytest -q tests/test_control_capability_policy.py` → PASS
- [ ] **Step 4: Commit** — `feat(control): central capability write_role policy (filters → staff)`

---

### Task 8: Server-side enforcement + persistence model + drift re-apply

**Files:**
- Modify: `apps/control/models.py` (new `PersistedCapability`), migration `apps/control/migrations/0004_persistedcapability.py` (makemigrations), `apps/control/admin.py` (register read-only list)
- Create: `apps/control/persistence.py`
- Modify: `apps/control/consumers.py` (`ControlConsumer._handle_command`, `ControlConsumer.control_result`, `AgentControlConsumer.receive` inventory branch)
- Test: `tests/test_control_write_role_enforcement.py`, `tests/test_control_persisted_caps.py`

**Interfaces:**
- Consumes: `capability_policy.policy_for`, `can_write`.
- Produces:
  - `PersistedCapability(station FK CASCADE, slot CharField(max_length=16), module_id CharField(64), capability CharField(64), value JSONField, updated_by FK User SET_NULL null, updated_at auto_now)`; `UniqueConstraint(fields=["station","slot","module_id","capability"], name="uniq_persisted_cap")`. (`slot` stored as `str(slot)` — matches how `apply_inventory` keys slots as strings; check `StationModule.slot` field type and use the same type.)
  - `persistence.save(station, slot, module_id, capability, value, user) -> None` (upsert).
  - `persistence.reapply_frames(station, slots) -> list[dict]` — for every inventory-reported module and every persisted row for it whose `capability` is in the module's descriptor (`kind == "setting"`, not readonly) and whose reported `state[cap] != value`, a frame `{"v": 1, "type": "command", "request_id": f"persist-{row.pk}-{uuid4().hex[:8]}", "slot": <slot as reported>, "module": module_id, "capability": cap, "op": "set", "value": value}`.
  - WS error for under-privileged set: `{"type":"error","request_id":rid,"error":{"code":"forbidden","msg":"Requires role: staff"}}`.

- [ ] **Step 1: Failing enforcement tests** — follow the communicator/lock pattern of `tests/test_control_consumer_relay.py` (it already connects a browser that holds the lock and asserts frames relayed to the agent group). Create a `StationModule(type="fm", capability_descriptor=[{"name":"filter_hpf","kind":"setting","type":"bool"},{"name":"frequency","kind":"setting","type":"int"}])`.

```python
# tests/test_control_write_role_enforcement.py  (sketch — reuse helpers from test_control_consumer_relay.py)
@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_operator_holding_lock_cannot_set_staff_capability(...):
    # member user acquires lock, sends raw command for filter_hpf
    await browser.send_json_to({"type": "command", "request_id": "r1", "slot": 1,
                                "module": "fm0", "capability": "filter_hpf", "op": "set",
                                "value": False})
    err = await _next_of_type(browser, "error")
    assert err["request_id"] == "r1" and err["error"]["code"] == "forbidden"
    assert await agent_group_received_nothing(...)       # NOT relayed
    assert StationAuditLog.objects.filter(event_type="control_command",
                                          message__contains="denied").exists()


async def test_operator_can_still_set_operator_capability(...):   # frequency relayed


async def test_staff_can_set_filter_and_it_is_relayed(...):


async def test_unknown_module_uses_default_policy_by_name(...):
    # filter_hpf on a module_id not in DB → policy (cap, None) still staff → forbidden for member
```

- [ ] **Step 2: Implement enforcement** in `ControlConsumer._handle_command`, after the lock check and BEFORE `_relay`:
```python
        capability = msg.get("capability")
        module_type = await self._module_type(station, msg.get("slot"), msg.get("module"))
        if not await self._may_write(station, capability, module_type):
            role = capability_policy.policy_for(capability, module_type).write_role
            await self._audit(station, "control_command",
                              f"{self.user.username} {msg.get('op')} {capability} denied (requires {role})")
            await self._error(msg.get("request_id"), "forbidden", f"Requires role: {role}")
            return
```
`_module_type` = `database_sync_to_async` lookup of `StationModule.objects.filter(station=station, slot=slot, module_id=module).values_list("type", flat=True).first()` (returns `None` if missing — the `(cap, None)` entry still applies, so an unknown module id cannot bypass). `_may_write` = `database_sync_to_async(capability_policy.can_write)(self.user, station, capability, module_type)`. Note `capability` may be `None` or non-str for malformed frames → `policy_for` returns default (operator) — keep today's behaviour; ensure `policy_for` doesn't crash on unhashable values: guard `if not isinstance(capability, str): return _DEFAULT` (add a test).
Also: if the policy has `persist=True` and `op == "set"`, remember `self._persist_pending[request_id] = (slot, module, capability, value)` (init dict in `connect`; pop in `disconnect`).
In `ControlConsumer.control_result`: `entry = self._persist_pending.pop(rid, None)`; if `entry and msg.get("ok") is True`: `await self._persist(station, *entry)` → `persistence.save(..., user=self.user)` + audit `control_command` "persisted <cap>=<value>".

- [ ] **Step 3: Failing persistence/re-apply tests**

```python
# tests/test_control_persisted_caps.py
import pytest

from apps.control import persistence
from apps.control.models import PersistedCapability, StationModule

DESC = [{"name": "filter_hpf", "kind": "setting", "type": "bool"},
        {"name": "filter_lpf", "kind": "setting", "type": "bool"},
        {"name": "rssi", "kind": "telemetry", "type": "int"}]


def _inv(state, module="fm0", slot=1, desc=DESC):
    return [{"slot": slot, "modules": [{"module": module, "identity": {"type": "fm"},
                                         "capabilities": desc, "state": state}]}]


@pytest.mark.django_db
def test_save_upserts(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    persistence.save(station, 1, "fm0", "filter_hpf", True, staff_user)
    assert PersistedCapability.objects.get().value is True


@pytest.mark.django_db
def test_reapply_only_on_drift(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    assert persistence.reapply_frames(station, _inv({"filter_hpf": False})) == []
    frames = persistence.reapply_frames(station, _inv({"filter_hpf": True}))
    assert len(frames) == 1
    f = frames[0]
    assert (f["type"], f["op"], f["slot"], f["module"], f["capability"], f["value"]) == (
        "command", "set", 1, "fm0", "filter_hpf", False)
    assert f["request_id"].startswith("persist-")


@pytest.mark.django_db
def test_reapply_when_state_missing_key(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_lpf", False, staff_user)
    assert len(persistence.reapply_frames(station, _inv({}))) == 1


@pytest.mark.django_db
def test_no_reapply_for_cap_not_in_descriptor_or_other_module(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    assert persistence.reapply_frames(station, _inv({}, desc=[])) == []
    assert persistence.reapply_frames(station, _inv({}, module="fm1")) == []


@pytest.mark.django_db
def test_other_station_rows_never_leak(station, other_station, staff_user):
    persistence.save(other_station, 1, "fm0", "filter_hpf", False, staff_user)
    assert persistence.reapply_frames(station, _inv({"filter_hpf": True})) == []


@pytest.mark.django_db
def test_garbage_inventory_is_ignored(station, staff_user):
    persistence.save(station, 1, "fm0", "filter_hpf", False, staff_user)
    for junk in (None, "x", [None], [{"slot": 1, "modules": "x"}], [{"modules": [{}]}]):
        assert persistence.reapply_frames(station, junk) == []
```
Plus one Channels test: agent consumer receives an `inventory` with drift → the agent communicator receives a `command` frame with `request_id` starting `persist-` (the AgentControlConsumer sends it to itself via `self.send`), and a browser-side `ok` result for a staff `filter_hpf` set creates a `PersistedCapability` row, while `ok: false` does not.

- [ ] **Step 4: Implement** `apps/control/persistence.py`:
```python
"""Per-station persistence + drift re-apply for role-gated calibration capabilities
(spec §4a). The FW persists nothing; the server stores the chosen value and, whenever an
agent inventory shows the module disagreeing (reconnect, module reboot, SA818 power-cycle),
sends a ``set`` through the agent. Idempotent: no drift → no frames."""

import uuid

from .models import PersistedCapability


def save(station, slot, module_id, capability, value, user):
    PersistedCapability.objects.update_or_create(
        station=station, slot=str(slot), module_id=module_id, capability=capability,
        defaults={"value": value, "updated_by": user},
    )


def _settable(desc, cap):
    return any(
        isinstance(d, dict) and d.get("name") == cap and d.get("kind") == "setting"
        and not d.get("readonly")
        for d in desc
    )


def reapply_frames(station, slots):
    if not isinstance(slots, list):
        return []
    rows = {}
    for r in PersistedCapability.objects.filter(station=station):
        rows.setdefault((r.slot, r.module_id), []).append(r)
    frames = []
    for entry in slots:
        if not isinstance(entry, dict) or not isinstance(entry.get("modules"), list):
            continue
        slot = entry.get("slot")
        for mod in entry["modules"]:
            if not isinstance(mod, dict):
                continue
            module_id = mod.get("module")
            desc = mod.get("capabilities") if isinstance(mod.get("capabilities"), list) else []
            state = mod.get("state") if isinstance(mod.get("state"), dict) else {}
            for row in rows.get((str(slot), module_id), []):
                if not _settable(desc, row.capability):
                    continue
                if row.capability in state and state[row.capability] == row.value:
                    continue
                frames.append({
                    "v": 1, "type": "command",
                    "request_id": f"persist-{row.pk}-{uuid.uuid4().hex[:8]}",
                    "slot": slot, "module": module_id, "capability": row.capability,
                    "op": "set", "value": row.value,
                })
    return frames
```
`AgentControlConsumer.receive` inventory branch, after `_apply_inventory`: `for frame in await self._reapply_frames(station, msg.get("slots", [])): await self.send(text_data=json.dumps(frame))` and one audit line per frame (`StationAuditLog.log(station=..., event_type="control_command", message=f"re-apply persisted {cap}={value}", user=None)` — check `StationAuditLog.log` accepts `user=None`; agent results for these `persist-*` ids are broadcast to browsers, which ignore unknown request ids — verify in `control-panel.js` result handler and add a guard if not).
Register `PersistedCapability` in `apps/control/admin.py` (list_display station/slot/module_id/capability/value/updated_by/updated_at).

- [ ] **Step 5: Run** `python -m pytest -q tests/test_control_*.py` → PASS
- [ ] **Step 6: Commit** — `feat(control): enforce capability write_role server-side; persist + re-apply calibration caps`

---

### Task 9: Role-aware render (read-only widgets)

**Files:**
- Create: `apps/control/templatetags/__init__.py`, `apps/control/templatetags/control_caps.py`
- Modify: `apps/control/templates/control/widgets/_widget.html`, `_bool.html`, `_number.html`, `_enum.html`; `static/css/…` (find where `.cp-widget` styles live: `grep -rn "cp-toggle" static/css`)
- Test: `tests/test_control_role_aware_render.py`

**Interfaces:**
- Consumes: `capability_policy.can_write`, `policy_for`.
- Produces: `{% load control_caps %}{% cap_access cap m as access %}` → dict `{"writable": bool, "write_role": str}` (takes_context: uses `context["request"].user` and `context["station"]`). Widgets: when `not access.writable` render the control with a static `disabled` + `aria-disabled="true"`, **no** `@click`/`@change`, plus `<span class="cp-cap-lock" title="…">{% trans "staff only" %}</span>` and the wrapper gets `data-readonly="true"`. Value display bindings stay (operator sees state).

- [ ] **Step 1: Failing tests** — render `StationControlView` with the Django test client (pattern from `tests/test_control_views.py`) for a member and a staff user, station with an `fm` module whose descriptor has `filter_hpf` (bool), `filter_pre_emphasis` (bool), `frequency` (int), plus a synthetic enum cap patched into `POLICY` as staff:

```python
def _widget_html(html, cap):
    import re
    m = re.search(rf'<div class="cp-widget[^"]*"[^>]*data-cap="{cap}"[^>]*>.*?</div>\s*(?=<div class="cp-widget|</div>\s*</article>)', html, re.S)
    assert m, f"widget {cap} not rendered"
    return m.group(0)


def test_member_sees_filter_read_only(client, member, station_with_fm):
    client.force_login(member)
    html = client.get(url).content.decode()
    w = _widget_html(html, "filter_hpf")
    assert 'data-readonly="true"' in w and "@click" not in w and " disabled" in w
    assert "cp-cap-lock" in w
    # operator capability unchanged
    f = _widget_html(html, "frequency")
    assert 'data-readonly' not in f and "@change" in f


def test_staff_sees_filter_editable(client, staff, station_with_fm):
    client.force_login(staff)
    w = _widget_html(client.get(url).content.decode(), "filter_hpf")
    assert 'data-readonly' not in w and "@click" in w


def test_number_and_enum_read_only_variants(client, member, station_with_fm, monkeypatch):
    # patch POLICY so 'frequency' (number) and 'mode' (enum) are staff → both render read-only
    ...
```
(If the regex helper is brittle, parse with `html.parser`/BeautifulSoup if it is already a test dependency — `grep -rn bs4 requirements*` — else keep a simple slice: find `data-cap="filter_hpf"`, cut until the next `data-widget`.) Also run the existing `tests/test_control_page_integrity.py` (template guard).

- [ ] **Step 2: Implement the tag**
```python
# apps/control/templatetags/control_caps.py
from django import template

from apps.control import capability_policy

register = template.Library()


@register.simple_tag(takes_context=True)
def cap_access(context, cap, module):
    request = context.get("request")
    station = context.get("station")
    name = cap.get("name") if isinstance(cap, dict) else None
    mtype = getattr(module, "type", None)
    policy = capability_policy.policy_for(name, mtype)
    user = getattr(request, "user", None)
    writable = station is not None and capability_policy.can_write(user, station, name, mtype)
    return {"writable": writable, "write_role": policy.write_role}
```
(Each widget include evaluates this once per capability — a handful of cached-property role checks per page; `can_administer_station` hits the DB per call — acceptable for a few caps, but compute `viewer_role` once: add `ctx["viewer_role"] = capability_policy.viewer_role(u, station)` in `StationControlView.get_context_data` and make the tag prefer `context["viewer_role"]` when present via a `rank` comparison helper `capability_policy.role_allows(role, write_role) -> bool`; add that helper + a unit test in Task 7's file.)

`_widget.html`: at the top `{% load control_caps %}{% cap_access cap m as access %}` — includes inherit the context so `access` is visible in the widget partials.
`_bool.html` (pattern; apply the same idea to `_number.html` buttons/input and `_enum.html` select):
```django
<div class="cp-widget cp-bool" ... {% if not access.writable %}data-readonly="true"{% endif %}>
  <label ...>{{ cap.name }}{% if not access.writable %} <span class="cp-cap-lock" title="{% blocktrans with role=access.write_role %}Changeable by {{ role }} only{% endblocktrans %}">&#128274;</span>{% endif %}</label>
  <button type="button" ... 
    {% if access.writable %}
      :disabled="!canOperate(...)"
      @click="setValue(...)"
    {% else %}
      disabled aria-disabled="true"
    {% endif %}
    ...>
```
Keep all `:class`/`:aria-checked`/`x-text` value bindings in both branches. Add CSS: `.cp-widget[data-readonly] { opacity: .75 } .cp-cap-lock { font-size: .8em; margin-left: .25rem }` in the stylesheet that defines `.cp-toggle`.

- [ ] **Step 3: Run** `python -m pytest -q tests/test_control_role_aware_render.py tests/test_control_views.py tests/test_control_page_integrity.py` → PASS
- [ ] **Step 4: Commit** — `feat(control): render role-gated capabilities read-only below write_role`

---

### Task 10: Browser capture (DSP off + fixed gain) and TX hub meter UI

**Files:**
- Modify: `static/js/audio-logic.js` (new pure functions + export), `static/js/audio-panel.js` (getUserMedia ~line 914, graph ~line 931-937, `_routeJSON` ~line 316, state fields ~line 106, teardown ~line 1344), `apps/control/templates/control/_audio_panel.html` (Microphone section ~line 254-346)
- Test: `tests/js/audio-logic.test.mjs` (append), `tests/test_audio_panel_tx_meter_template.py`

**Interfaces:**
- Consumes: browser `tx_meter` frame (Task 6).
- Produces (audio-logic.js, exported): `MIC_CAPTURE_GAIN_DB = 6`, `micCaptureConstraints() -> {channelCount:1, echoCancellation:false, noiseSuppression:false, autoGainControl:false}`, `captureGainLinear() -> number`, `txMeterView(msg) -> {active, hubFrac (0..1), peakDbfs, grDb, limiting, degraded, ceilingDbfs}` where `hubFrac = clamp((peak - (ceiling-30)) / 30, 0, 1)` (bar spans the 30 dB below the ceiling; full bar = at the ceiling), inactive/garbage → `{active:false, hubFrac:0, limiting:false, degraded:false, ...nulls}`.

- [ ] **Step 1: Failing JS tests** (append to `tests/js/audio-logic.test.mjs`, matching its assert style):
```js
// micCaptureConstraints: native DSP OFF (spec §3.1)
{
  const c = A.micCaptureConstraints();
  assert.strictEqual(c.echoCancellation, false);
  assert.strictEqual(c.noiseSuppression, false);
  assert.strictEqual(c.autoGainControl, false);
  assert.strictEqual(c.channelCount, 1);
}
// fixed capture gain
assert.ok(Math.abs(A.captureGainLinear() - Math.pow(10, A.MIC_CAPTURE_GAIN_DB / 20)) < 1e-9);
// txMeterView
{
  const v = A.txMeterView({type: "tx_meter", active: true, peak_dbfs: -12, ceiling_dbfs: -12,
                           gain_reduction_db: 4, limiting: true, dsp: "full"});
  assert.strictEqual(v.hubFrac, 1);
  assert.strictEqual(v.limiting, true);
  assert.strictEqual(v.degraded, false);
  const q = A.txMeterView({active: true, peak_dbfs: -27, ceiling_dbfs: -12, dsp: "degraded"});
  assert.ok(Math.abs(q.hubFrac - 0.5) < 1e-9);
  assert.strictEqual(q.degraded, true);
  const silent = A.txMeterView({active: true, peak_dbfs: null, ceiling_dbfs: -12, dsp: "full"});
  assert.strictEqual(silent.hubFrac, 0);
  for (const junk of [null, undefined, "x", {active: false}, {active: true, peak_dbfs: "x"}]) {
    const j = A.txMeterView(junk);
    assert.ok(j.hubFrac >= 0 && j.hubFrac <= 1 && !Number.isNaN(j.hubFrac));
  }
  // missing ceiling falls back to the safe default -12
  assert.strictEqual(A.txMeterView({active: true, peak_dbfs: -12, dsp: "full"}).hubFrac, 1);
}
```

- [ ] **Step 2: Implement pure functions** in `audio-logic.js` (near `captureConstraintsFromSettings`), add to the returned api object:
```js
  /* Browser capture: native DSP OFF (spec §3.1). The VAD noise suppressor gated word-ends;
     AGC/EC fought the agent DSP, which is now the single level authority. */
  var MIC_CAPTURE_GAIN_DB = 6;
  function micCaptureConstraints() {
    return { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false };
  }
  function captureGainLinear() { return dbfsToAmplitude(MIC_CAPTURE_GAIN_DB); }

  var TX_METER_SPAN_DB = 30;
  var TX_CEILING_DEFAULT_DBFS = -12;
  function _finite(x) { return typeof x === "number" && isFinite(x); }
  function txMeterView(msg) {
    var off = { active: false, hubFrac: 0, peakDbfs: null, grDb: 0, limiting: false, degraded: false, ceilingDbfs: null };
    if (!msg || typeof msg !== "object" || !msg.active) return off;
    var ceiling = _finite(msg.ceiling_dbfs) ? msg.ceiling_dbfs : TX_CEILING_DEFAULT_DBFS;
    var peak = _finite(msg.peak_dbfs) ? msg.peak_dbfs : null;
    var frac = peak === null ? 0 : (peak - (ceiling - TX_METER_SPAN_DB)) / TX_METER_SPAN_DB;
    frac = Math.max(0, Math.min(1, frac));
    return {
      active: true, hubFrac: frac, peakDbfs: peak,
      grDb: _finite(msg.gain_reduction_db) ? msg.gain_reduction_db : 0,
      limiting: !!msg.limiting, degraded: msg.dsp === "degraded" || msg.dsp === "off",
      ceilingDbfs: ceiling,
    };
  }
```

- [ ] **Step 3: Wire audio-panel.js**
  - `getUserMedia({ audio: A.micCaptureConstraints() })` (replace the literal at ~line 914).
  - State: `_micCaptureGain: null`, and reactive `txMeter: A.txMeterView(null)`.
  - Graph: after creating `_micSource`: `self._micCaptureGain = micCtx.createGain(); self._micCaptureGain.gain.value = A.captureGainLinear(); self._micSource.connect(self._micCaptureGain); self._micCaptureGain.connect(self._micWorkletNode);` replacing `self._micSource.connect(self._micWorkletNode)`. Leave the analyser/sidetone taps on `_micSource` and add the comment: `// NOTE: the input meter + sidetone tap the RAW mic (pre-gain, pre-encode). They do NOT prove the transmitted level — the authoritative TX level is the tx_meter from the agent (post-DSP, spec §3.5).`
  - Teardown (~line 1344): disconnect + null `_micCaptureGain`; reset `this.txMeter = A.txMeterView(null)` when mic closes or WS closes.
  - `_routeJSON`: `case "tx_meter": this.txMeter = A.txMeterView(msg); break;`
  - If the browser T1 diagnostic tap (`buildTapReport("T1", …)`, ~line 961) taps the worklet input, note in a comment that it now includes the fixed capture gain (no code change).

- [ ] **Step 4: Template** — in `_audio_panel.html` Microphone section, below the mic input meter, add (use `frontend-design` skill for the visual; keep the existing `cp-meter-track`/`cp-meter-fill` classes):
```django
{% comment %}
TX modulation meter (spec §3.5): what is actually transmitted (post-DSP at D), not the mic.
Full bar = at the per-station limiter ceiling. Read-only operator feedback.
{% endcomment %}
<div class="cp-tx-meter" x-show="txMeter.active" data-tx-meter>
  <div class="cp-meter-label">{% trans "TX modulation" %}</div>
  <div class="cp-meter-track" role="meter" aria-valuemin="0" aria-valuemax="100"
       :aria-valuenow="Math.round(txMeter.hubFrac * 100)" aria-label="{% trans 'TX modulation' %}">
    <div class="cp-meter-fill" :class="{ 'cp-meter-fill--limit': txMeter.limiting }"
         :style="'width:' + Math.round(txMeter.hubFrac * 100) + '%'"></div>
  </div>
  <span class="cp-badge cp-badge--limit" x-show="txMeter.limiting">{% trans "Limiting" %}</span>
  <span class="cp-badge cp-badge--warn" x-show="txMeter.degraded"
        title="{% trans 'Agent DSP unavailable on this image — pass-through, no limiter' %}">{% trans "DSP off" %}</span>
</div>
```
Template test (`tests/test_audio_panel_tx_meter_template.py`): render the control page for a member; assert `data-tx-meter` present, `txMeter.hubFrac` bound, no `{#` multi-line leak (existing integrity test covers the guard).

- [ ] **Step 5: Run** `python -m pytest -q tests/test_audio_logic_js.py tests/test_audio_panel_tx_meter_template.py tests/test_control_page_integrity.py` → PASS; `node --check static/js/audio-panel.js`.
- [ ] **Step 6: Commit** — `feat(audio-ui): clean mic capture (NS/AGC/EC off + fixed gain) and live TX modulation meter`

---

### Task 11: Calibration runbook + full verification

**Files:**
- Create: `docs/audio/tx-calibration-runbook.md`

- [ ] **Step 1: Runbook** (spec §6) — sections: Preconditions (station on an image with `gstreamer1.0-plugins-good-audiofx`; meter shows no "DSP off"), Step 1 harness anchor U with DSP in path (read C/D dBFS; no keying), Step 2 on-air A/B vs a reference station with a deviation meter, adjust `TX audio ceiling (dBFS)` on the station edit page (staff) — heartbeat pushes it within one interval, next TX start applies it, Step 3 SA818 filters via the module card (staff) or `sa818 at filters` shell; default config = SA818 pre-emph ON + HPF/LPF ON (agent pre-emphasis not available with gst built-ins), Step 4 record value + date in the station log. Known limitations: static (memoryless) dynamics, no gate hang; degraded mode = unity pass-through.
- [ ] **Step 2: Full suite + lint**
Run: `python -m pytest -q` → all PASS; `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .` → clean; `python manage.py makemigrations --check --dry-run` → "No changes detected".
- [ ] **Step 3: Commit** — `docs(audio): TX calibration runbook`

---

## Follow-ups (not in this PR — report to coordinator)

1. **linux-image:** add `gstreamer1.0-plugins-good-audiofx` to `oe5xrx-audio-system` RDEPENDS (`audiocheblimit`, `audiodynamic`); verify `volume` is pinned explicitly (`gstreamer1.0-plugins-base-volume`) instead of relying on `-base` meta RRECOMMENDS. Until then stations run degraded (unity pass-through, meter shows "DSP off").
2. Agent-side pre-emphasis (spec §5 preferred config) and envelope-based gate/compressor with attack/release/hang — need a non-gst-launch DSP path (python-gi appsrc or in-Python DSP); decide after on-air calibration.
3. Per-station `write_role` overrides (spec §4a YAGNI).
