# Audio-Path Diagnostics — Usage Recipe

**Audience:** AI agents, operators, and integration scripts.
**Scope:** Digital TX chain A–D (Browser → agent → PipeWire → ALSA/UAC2). No RF keying.

---

## REST API

### Endpoint

```
POST /api/v1/stations/{id}/audio-diagnostics/
Authorization: Bearer <PAT>
Content-Type: application/json
```

**Request body:**

```json
{
  "anchor": "C",
  "slot": 1,
  "signal": {
    "kind": "sine",
    "freq_hz": 1000,
    "level_dbfs": -20.0,
    "duration_ms": 500
  }
}
```

| Field | Values | Default | Notes |
|-------|--------|---------|-------|
| `anchor` | `"C"`, `"U"` | `"C"` | Inject point. Both anchors are production-supported via this endpoint. |
| `slot` | integer | optional (default 0) | Station hardware slot number |
| `signal.kind` | `"sine"` | `"sine"` | Reference signal type |
| `signal.freq_hz` | integer | `1000` | Test tone frequency |
| `signal.level_dbfs` | float | `-20.0` | Inject level, dBFS peak |
| `signal.duration_ms` | integer | `500` | Measurement window (max 5000 ms) |

### curl example

```bash
curl -s -X POST \
  "https://station-manager.example.com/api/v1/stations/211/audio-diagnostics/" \
  -H "Authorization: Bearer <your-PAT>" \
  -H "Content-Type: application/json" \
  -d '{"anchor":"C","slot":1,"signal":{"kind":"sine","freq_hz":1000,"level_dbfs":-20.0,"duration_ms":500}}'
```

### Response

HTTP 200 — diagnostic run report:

```json
{
  "anchor": "C",
  "reference": {"freq_hz": 1000, "level_dbfs": -20.0, "window_ms": 300},
  "taps": [
    {
      "point": "C",
      "format": {"rate": 16000, "channels": 1},
      "rms_dbfs": -20.5,
      "peak_dbfs": -20.0,
      "window_ms": 300,
      "silent": false,
      "computed": false
    },
    {
      "point": "D",
      "format": {"rate": 16000, "channels": 1},
      "rms_dbfs": -28.5,
      "peak_dbfs": -28.0,
      "window_ms": 300,
      "silent": false,
      "computed": true
    }
  ],
  "static_gains": {
    "sink_volume_linear": 0.4,
    "sink_volume_db": -7.96,
    "resample": "48k<->8k",
    "note": "sink_volume_db is the C->D static gain stage (spec: sink vol 0.40 ~= -8 dB)"
  }
}
```

**Status codes:**

| Code | Meaning |
|------|---------|
| `200` | Run complete; inspect `taps` for deltas |
| `400` | Bad anchor/slot/signal (e.g. unknown anchor, slot out of range, unsupported signal kind) |
| `403` | Applicant membership or insufficient access level |
| `404` | Station ID not found or not in caller's topology scope |
| `409` | Station busy — another diagnostic (e.g. operator PTT or in-flight anchor-U run) is active; retry when the station is idle |
| `503` | Agent offline / not connected |
| `504` | Diagnostic timed out waiting for agent result |

---

## Bisection Loop (AI/Operator Interpretation)

The diagnostic measures the digital TX chain using anchor C or anchor U, both
production-ready via this endpoint.

### Anchor C — agent/station sub-chain (production)

Agent uses `audiotestsrc` directly → PipeWire sink → ALSA. Tests the
station-internal path from the GStreamer inject point through PipeWire and to the
ALSA/UAC2 boundary. Does not cover the relay or Opus decode path.

**Interpret the result:**

1. **Check C (before PipeWire sink volume):**
   - C close to the injected level (e.g. −20 dBFS inject → C ≈ −20 to −22 dBFS): the
     agent and GStreamer inject path are clean.
   - C is much lower (> 6 dB below inject level): loss is upstream — in the GStreamer
     graph or the PipeWire source node.

2. **Check C→D (sink volume stage):**
   - On real HW, tap D is **projected** as C + `static_gains.sink_volume_db` (not
     independently measured). The C→D delta therefore equals the known sink volume by
     construction and **cannot** reveal extra loss in the ALSA driver path beyond what
     the sink volume accounts for.
   - To detect extra loss between C and D (e.g. an unexpected ALSA driver attenuation),
     a direct reverse or loopback tap at D would be required (not currently implemented).
   - The verdict reflects this: when D is projected, it is labelled "D not independently
     measured".

3. **Summary decision tree:**
   ```
   C ≈ inject level                        →  digital chain healthy to C; D projected from C + sink_db
   C << inject level (> 6 dB loss)         →  loss upstream of C (GStreamer/PipeWire source)
   C silent                                →  agent TX node not found / GStreamer failed
   ```

### Anchor U — server-originated headless reference (production)

Server injects a calibrated sine reference into the op.mic uplink toward the agent,
replacing the browser at its protocol boundary. Tests the full relay → agent → GStreamer
→ PipeWire sink → ALSA path without a real browser — the entire digital chain U→C→D is
covered by a single run, with no browser session required.

**Idle-station requirement:** Anchor U holds exclusive control of the agent's TX path for
the duration of the run. If another diagnostic or an operator PTT event is already active,
the endpoint returns `409 {"detail": "station busy"}`. Retry when the station is idle.

**RF-safety:** Anchor U terminates at the PipeWire sink / ALSA boundary (tap D) exactly
as anchor C does. It does **not** key the SA818 transmitter, does **not** generate RF,
and does **not** assert PTT.

**Request example:**

```bash
curl -s -X POST \
  "https://station-manager.example.com/api/v1/stations/211/audio-diagnostics/" \
  -H "Authorization: Bearer <your-PAT>" \
  -H "Content-Type: application/json" \
  -d '{"anchor":"U","slot":1}'
```

---

## On-Station CLI (Direct Debugging)

For direct debugging on station 211 (or any station running the station-agent):

```bash
python -m station_agent selftest audio-diag --slot 1 --anchor C
```

With options:

```bash
python -m station_agent selftest audio-diag \
  --slot 1 \
  --anchor U \
  --freq 1000 \
  --level-dbfs -20.0 \
  --duration-ms 500
```

Prints the JSON run report to stdout. Useful when:
- SSH'd directly onto the station for manual validation
- Verifying a PipeWire node resolves correctly for a given slot
- Checking the sink volume is as expected without a full REST round-trip

**Options:**

| Option | Default | Notes |
|--------|---------|-------|
| `--slot N` | `1` | Hardware slot (sim=1, bench=3) |
| `--anchor C\|U` | `C` | Inject point |
| `--freq HZ` | `1000` | Sine frequency |
| `--level-dbfs DBFS` | `-20.0` | Inject level, dBFS peak |
| `--duration-ms MS` | `500` | Measurement duration |

---

## RF-Safety

The entire A–D diagnostic is **purely digital**. Inject points U and C terminate at
the PipeWire sink / ALSA/UAC2 boundary (tap D). The diagnostic:

- Does **not** key the SA818 transmitter
- Does **not** generate RF
- Does **not** assert PTT

The digital sample stream at D flows toward the USB UAC2 interface regardless of PTT
state; the SA818 only modulates if separately keyed via the control plane. Diagnostic
runs never touch the control plane.

---

## Uniform Schema Reference

**Tap dict** (each entry in `taps[]`):

- `point`: `"C"` or `"D"` (measurement point label)
- `format`: `{"rate": <int>, "channels": 1}` (PCM parameters)
- `rms_dbfs`: RMS level in dBFS, or `null` if silent
- `peak_dbfs`: Peak level in dBFS, or `null` if silent
- `window_ms`: Measurement window duration (trailing portion of capture)
- `silent`: `true` if no signal detected (peak == 0 or empty capture)
- `computed`: `true` if derived from C + sink gain (D on real HW); `false` if measured directly

**Inject request** (POST body `signal` field):

- `kind`: `"sine"` (WAV support is a future extension)
- `freq_hz`: tone frequency in Hz
- `level_dbfs`: peak level in dBFS (negative; e.g. `−20.0`)
- `duration_ms`: total signal duration including settle lead-in (max 5000 ms)

**Calibration constants** (from `station_agent.audio.diagnostics`):

- Reference frequency: 1000 Hz
- Reference level: −20.0 dBFS (peak)
- Measurement window: 300 ms (trailing)
- Settle lead-in: 200 ms (discarded from front of capture)

**Run report** (full response):

- `anchor`: inject point used (`"U"` or `"C"`)
- `reference`: `{freq_hz, level_dbfs, window_ms}` — what was injected
- `taps`: list of tap dicts (C and D)
- `static_gains`: `{sink_volume_linear, sink_volume_db, resample, note}`
- `error` (only on failure): string describing what went wrong
