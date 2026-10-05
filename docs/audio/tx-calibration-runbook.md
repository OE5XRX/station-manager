# TX audio calibration runbook

One-time per board (re-run after a board/SA818/image change). Goal: find the per-station
**TX audio ceiling (dBFS)** that yields the target FM deviation (±2.5 kHz NBFM / ±5 kHz WBFM
as applicable) without over-deviating. Design: `docs/superpowers/specs/2026-10-03-tx-audio-leveling-design.md`.

**RF safety:** the DSP, the TX meter, the SA818 filter set and the calibration measurement
(Step 1) are purely digital and never key the SA818. The diagnostic endpoint terminates at the
PipeWire sink / ALSA boundary; the RF interlock refuses diagnostics while a real TX/PTT
session is active. Only Step 2 transmits (real PTT, on-air or dummy load).

## What is built

TX chain in the agent (`station_agent/audio/tx_dsp.py`), all F32, between `audioresample`
and `pipewiresink`:

band-pass (HPF 300 Hz / LPF 3 kHz, 4 poles) -> expander gate -> hard-knee compressor
(policy 3:1) -> makeup gain -> hard-knee limiter (policy 1000:1) as the **last** stage.

The limiter threshold is the station ceiling; makeup is derived from it
(`ceiling - 3 dB headroom - (-24 dBFS nominal compressed peak)`).

Maintainer note: policy ratios are human N:1. GStreamer `audiodynamic` computes
`thr + (x - thr) * ratio`, so the pipeline gets `ratio = 1/N` (a ratio > 1 would amplify).

Browser capture: native noise suppression / AGC / echo cancellation are OFF; a fixed capture
GainNode at 0 dB (unity). `MIC_CAPTURE_GAIN_DB` in `static/js/audio-logic.js` is a
calibration knob: raise it only after on-station calibration, because the agent's `opusdec`
outputs S16 and a hot mic would clip before reaching the limiter.

Calibration value: Station field **TX audio ceiling (dBFS)** (`tx_audio_ceiling_dbfs`,
staff-only on the station edit page). Clamped to **[-24, -3] dBFS**; unset = **-12**
(errs toward under-deviation). Server pushes it in the heartbeat response; it is applied at
the **next TX start** (the agent clamps again).

## Preconditions

- Station image contains `gstreamer1.0-plugins-good-audiofx` (provides `audiocheblimit`,
  `audiodynamic`) and the `volume` element. Without them the agent runs **degraded**
  (unity pass-through, no limiter).
- In the audio panel, the TX meter shows **no "DSP off" pill** during a TX (check this once
  with a short PTT, or via the meter frames of a diagnostic).
- You have staff rights (ceiling field and filter capabilities are staff-only), a
  deviation-meter or a known-good reference station, and an idle station (no other
  PTT/diagnostic active, otherwise the API returns 409).

## Step 1 - harness reference with DSP in the path (no keying)

Run anchor **U** (server-originated sine reference, see `docs/audio-diagnostics-usage.md`):

```bash
curl -s -X POST "https://<server>/api/v1/stations/<id>/audio-diagnostics/" \
  -H "Authorization: Bearer <PAT>" -H "Content-Type: application/json" \
  -d '{"anchor":"U","slot":1}'
```

Read **C** and **D** peak/rms dBFS. With the DSP active, a -20 dBFS reference is lifted by
makeup and clipped at the ceiling; D should not exceed the configured ceiling (plus sink
volume). `computed:true` at D is expected. This never keys the SA818. If the run reports 409,
the station is busy: retry when idle.

## Step 2 - on-air A/B against a reference

1. Set an initial **TX audio ceiling (dBFS)** on the station edit page (e.g. -12).
2. Wait one heartbeat interval; the new value is applied at the next TX start (end and
   restart the PTT to pick it up).
3. Transmit speech (or a steady tone) and compare against a known-good reference station
   with a deviation meter. Watch the TX meter in the audio panel (visible to the
   transmitting operator): hub bar spans 30 dB below the ceiling, full bar = at the ceiling;
   the peak dBFS readout; the "Limiting -x dB" pill shows limiter gain reduction.
4. Raise/lower the ceiling (0.5 dB steps) until peak deviation matches the target
   (+-2.5 kHz NBFM / +-5 kHz WBFM). Prefer under- over over-deviation.

Meter warnings:

- **DSP off** - degraded: pass-through at unity, no limiter. Fix the image (see Known
  limitations); do not calibrate a ceiling in this state, it has no effect.
- **TX audio pipeline failed** - the pipeline died even without DSP; TX audio is not flowing.

## Step 3 - SA818 filters

Filters are role-gated capabilities (`filter_pre_emphasis`, `filter_hpf`, `filter_lpf`;
`write_role` staff). On the module card (staff) the controls are writable; operators see
them read-only. Alternative: the `sa818 at filters` debug shell command on the station.

After a successful set the value is persisted per station and re-applied by the server on
drift (not during an active PTT). Server-side enforcement at the set path is the real gate.

Default configuration: SA818 **pre-emphasis ON**, **HPF ON**, **LPF ON**. The agent applies
no pre-emphasis itself (not available with gst built-ins), so the SA818 internal
pre-emphasis/limiter is the sibilant backstop. Try pre-emphasis on/off during Step 2 and keep
what sounds and measures right; the agent LPF (3 kHz) plus the SA818 LPF band-limit the clip
products.

## Step 4 - record

Write the final ceiling value, date, operator, filter configuration and reference used into
the station log (station notes). The value itself lives in the station field.

## Known limitations

- Dynamics are static (memoryless per-sample curves of `audiodynamic`): no attack/release;
  the limiter is effectively a clipper at the ceiling and the compressor a static curve.
- The expander gate has no hang time; syllable tails can be chopped / noise can pump.
- Degraded mode (missing gst elements) is a unity pass-through with no limiter/makeup; the
  meter shows "DSP off". A station on an image without `audiofx` stays degraded until the
  image is fixed (linux-image follow-up: add `gstreamer1.0-plugins-good-audiofx` to
  `oe5xrx-audio-system` RDEPENDS and pin the `volume` element explicitly).
- A DSP pipeline that dies after the 0.3 s startup grace is not auto-restarted; the
  failure only surfaces as lost TX audio. Needs HIL validation on a real station.
- Agent-side pre-emphasis and envelope-based gate/compressor are follow-ups, to be decided
  after on-air calibration.
- Per the serial-boundary rule, an end-to-end claim on real hardware needs on-station
  confirmation; the sim does not reproduce SA818 deviation.
