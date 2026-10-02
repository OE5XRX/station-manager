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
  "anchor": "U",
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
| `anchor` | `"U"` or `"C"` | required | Inject point (see below) |
| `slot` | integer | required | Station hardware slot number |
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
  -d '{"anchor":"U","slot":1,"signal":{"kind":"sine","freq_hz":1000,"level_dbfs":-20.0,"duration_ms":500}}'
```

### Response

HTTP 200 — diagnostic run report:

```json
{
  "anchor": "U",
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
| `400` | Bad anchor/slot (e.g. unknown anchor, slot out of range) |
| `403` | Applicant membership or station not in caller's topology scope |
| `404` | Station ID not found or not in scope |
| `503` | Agent offline / not connected |
| `504` | Diagnostic timed out waiting for agent result |

---

## Bisection Loop (AI/Operator Interpretation)

The diagnostic bisects the digital TX chain with two anchor modes:

### Anchor U — headless, server-originated reference (AI path)

Server injects a calibrated sine reference into the op.mic uplink toward the agent,
replacing the browser at its protocol boundary. Tests the full relay → agent → GStreamer
→ PipeWire sink → ALSA path without a real browser.

**Interpret the result:**

1. **Check C (before PipeWire sink volume):**
   - C close to the injected level (e.g. −20 dBFS inject → C ≈ −20 to −22 dBFS): the
     agent and GStreamer decode path are clean.
   - C is much lower (> 6 dB below inject level): loss is upstream — in the relay,
     Opus encode/decode, or the agent RTP receive path.

2. **Check C→D delta:**
   - The PipeWire sink volume on station 211 is `0.40` (≈ −8 dB). D = C + sink_volume_db.
   - If the computed D delta matches `static_gains.sink_volume_db`, the C→D stage is
     accounted for: the only loss between C and D is the intentional sink attenuation.
   - A larger C→D delta indicates an additional loss in the sink or ALSA driver path.

3. **Summary decision tree:**
   ```
   C ≈ inject level AND D = C + sink_db  →  digital chain clean; loss is analog (E/F)
   C ≈ inject level AND D >> sink_db loss →  extra loss in sink/ALSA stage
   C << inject level                       →  loss upstream of C (relay/Opus/agent)
   C silent                                →  agent TX node not found / GStreamer failed
   ```

### Anchor C — agent/station sub-chain only

Agent uses `audiotestsrc` directly → PipeWire sink → ALSA. Tests only the
station-internal path (not the relay or Opus decode). Use anchor C to isolate
whether a loss measured with anchor U is in the station or in the wire/relay.

**Comparison:**
- `U` shows a loss at C, but `C` shows C at full level → loss is in the relay or
  Opus path between server and agent.
- Both `U` and `C` show the same loss at C → loss is in the station's GStreamer graph.

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
