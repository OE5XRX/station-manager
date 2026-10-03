# TX Audio Leveling & Deviation Control — Design

**Date:** 2026-10-03
**Status:** Draft (design) — awaiting user review before writing-plans
**Scope:** TX only (operator → SA818 → air). RX stays as-is (already works well).
**Repos touched:** `station-manager` (browser JS, server, station_agent), `linux-image` (WirePlumber), `FW-RemoteStation` (new SA818 FilterCap).

---

## 1. Problem & Intent

Operating the remote station over the web, the transmitted FM signal is **far too quiet and barely intelligible on air**. Two independent causes were confirmed on the real test station (.211) via the diagnostics harness (#153) and on-air listening:

1. **Loudness** — the PipeWire TX sink sits at **volume 0.40 (−8 dB)** and there is **no makeup-gain stage anywhere** in the TX chain. A calibrated −20 dBFS reference passes the agent gst output (**C**) clean and loses exactly the 0.40 sink (−8 dB) to the ALSA/UAC2 edge (**D**). Harness-quantified.
2. **Intelligibility** — word-ends are swallowed to near-silence. Cause: the browser's `getUserMedia` runs **`noiseSuppression:true` + `autoGainControl` (Chrome default on) + `echoCancellation:true`**; the VAD-based noise suppressor gates the quiet tails of words. Confirmed by ear.

**Intent:** present a **consistent, bounded FM deviation at the SA818** regardless of operator, microphone, or browser — so transmitted audio is reliably loud enough, intelligible, and never over-deviates (splatter / adjacent-channel / "good neighbour").

**Success criteria:**
- With a nominal operator over the web, the signal at the SA818 MIC reaches correct FM deviation (target hub), consistently, independent of operator mic level.
- Word-ends are no longer gated away.
- No over-deviation on loud operators / sibilants (bounded by a limiter).
- Verifiable **headless** (diagnostics harness, dBFS deltas) **and live** (operator-facing TX modulation meter).
- No code path keys the SA818 during measurement/leveling setup (RF safety).

**Non-goals (explicit):** RX leveling; a slow sentence-scale outer AGC/leveler (pump risk); per-operator profiles; over-deviation alerting integration; browser clip warning. Deferred unless a concrete need appears.

---

## 2. Signal Chain (target)

```
Browser:  mic → [native DSP OFF] → [fixed capture gain] → worklet(20ms) → Opus encode (16 kHz)
            │   (operator input meter + sidetone tap the raw mic, unchanged)
Relay:    station-manager  — dumb byte-identical Opus relay (UNCHANGED; exonerated by #153)
Agent:    udpsrc → rtpjitterbuffer → rtpopusdepay → opusdec → audioconvert → audioresample
            → [band-pass ~300–3000 Hz]
            → [gate/expander]          ← silence/noise down (level-based, NOT VAD)
            → [compressor]             ← evens loud→quiet (lifts tails)
            → [makeup gain]
            → [limiter]                ← hub ceiling (THE deviation authority)
            → pipewiresink → Sink (unity, no longer 0.40) → ALSA/UAC2 → DAC → SA818 MIC
SA818:    filters (pre-emphasis / HPF / LPF) — now agent-settable via new FilterCap
```

**Principle: exactly one authority for level/deviation = the agent DSP.** The browser only captures cleanly; the PipeWire sink is neutral (unity); the firmware filters are set deliberately (not left at power-on default); the SA818 internal limiter is at most a backstop.

---

## 3. Components

### 3.1 Browser capture (`station-manager`, `static/js/audio-panel.js`)
- `getUserMedia` constraints → `echoCancellation:false, noiseSuppression:false, autoGainControl:false`. Removes the word-end gating. (This is the confirmed intelligibility fix.)
- Insert one **fixed** `GainNode` between `_micSource` and the mic worklet (`_micSource → captureGain → _micWorkletNode`). Purpose: a healthy, predictable Opus-encode level / SNR — **not** level authority, **not** adaptive. Value is policy (see §4).
- Operator input meter (`_micAnalyser`) and sidetone tap the raw mic as today — unchanged. **Note in code:** these taps do NOT prove the transmitted level (they sit before encode); the authoritative meter is §3.5.

### 3.2 Agent DSP (`station-manager`, `station_agent/audio/opus_bridge.py` `build_tx_argv`) — the level authority
Insert, between `audioresample` and `pipewiresink`, using gst built-ins (Approach 1, **no new image dependency**):
- **Band-pass** ~300 Hz HPF + ~3 kHz LPF (voice band; keeps occupied bandwidth tight, stops sub-bass eating deviation).
- **Gate/expander** (`audiodynamic mode=expander`): closes only in true silence. Threshold = measured noise floor + margin; **hang time + slow release** so speech tails survive. This is the "keyed-but-silent must not transmit amplified noise" guard — and is **level-based, not VAD** (the explicit difference from the browser NS that caused the bug).
- **Compressor** (`audiodynamic mode=compressor`): evens syllable dynamics, lifts quiet word-ends via makeup. Start point ~3:1, moderate attack/release. No gate behaviour here — gating is the expander's job.
- **Makeup gain** (`volume`).
- **Limiter** (`audiodynamic`, hard-knee, high ratio, threshold at the ceiling): the **deviation ceiling**. Must sit **last** (after band-pass/compressor/makeup) so nothing downstream re-introduces peaks — see §5 pre-emphasis placement.

Degradation: if any element is unavailable at runtime (image built without it) or fails to construct, the pipeline falls back to **pass-through + the fixed makeup gain** so **TX never breaks entirely**; the condition is logged and surfaced in the meter (§3.5).

### 3.3 PipeWire sink neutralized (`linux-image`, WirePlumber)
- Remove the arbitrary **0.40** default on the FM TX sink → **unity (1.0)**. Level is set deliberately by the agent DSP, not by a magic sink number. One authority, not two. (The 0.40 default currently also lands on the built-in sink — scope the change to the FM module sink.)

### 3.4 SA818 FilterCap (`FW-RemoteStation`) — make filters agent-settable
- The SA818 supports `AT+SETFILTER=<pre-emphasis>,<HPF>,<LPF>` (each 0/1). The firmware already implements it (`sa818_at_set_filters`, `sa818_at.h` flags) **but only as a debug shell command** (`sa818 at filters …`); it is **not a module capability** and is **never set at boot**, so it runs at the SA818 power-on default and the agent cannot control it.
- Add a **`FilterCap`** to `subsys/module/devices/sa818/sa818_module.cpp` — one `Setting` subclass + one `g_caps[]` entry (the documented "add a capability = one subclass + one registry entry" pattern) — exposing the three filter flags in `module describe` so the agent/station-manager can set them. Working-state shadow in `Sa818Context` like the other caps (not persisted in FW; the agent owns persistence).
- The existing `sa818 at filters` debug shell stays — it is the **manual calibration tool** (§6).

### 3.5 TX modulation meter (the path back to the operator) (`station-manager`)
- While TX is active, the agent computes the post-DSP level at **D** (pre-SA818) + the limiter gain-reduction periodically (~5–10 Hz) and pushes it over the existing audio WS. Reuses the dBFS metric from #153.
- `_audio_panel.html`: a live **modulation/hub bar** ("how loud am I going out") + a **"limiting active"** indicator. This is the TX counterpart to today's mic input meter; it reflects **what is actually transmitted**, not just the mic.
- Terminology note: an "S-meter" is RX (received strength); on TX this is a **modulation/deviation meter**.

---

## 4. Control plane (hybrid)

Two parameter kinds:
- **Policy** (same for all stations): band-pass corners, gate threshold-margin/hang/release, compressor ratio/threshold/attack/release, limiter behaviour, browser fixed-gain, SA818 filter flags default. Lives as **defaults in the agent/image config** (`station_agent` config + WirePlumber in `linux-image`). Rarely touched.
- **Calibration** (per board/SA818 — a hardware property): **one number** — the input-gain / limiter-ceiling that maps to the target FM deviation at this board's SA818. Lives as a **new nullable field on the `Station` model** in station-manager, pushed to the agent via the **existing heartbeat response**; the agent applies it when it (re)starts the TX bridge. **Fallback to the policy default** when unset.

**Safety on the calibration value:** when unset or out of a safe range it is **clamped**, and the default errs toward **under-deviation** (quieter) rather than over-deviation. An uncalibrated station must **never** over-deviate/splatter.

MVP keeps it to the one calibration number (not a full per-station parameter set) — YAGNI.

---

## 5. Pre-emphasis placement (key design dependency)

FM uses TX pre-emphasis + RX de-emphasis. The **deviation limiter must sit AFTER pre-emphasis**, else pre-emphasis-boosted sibilants over-deviate past the limiter's ceiling.

With the FilterCap (§3.4) the agent controls the SA818 pre-emphasis, giving two viable configurations — decided/validated during on-station calibration (§6):

- **Preferred (textbook-correct):** SA818 pre-emphasis **OFF**, SA818 HPF/LPF **ON** (cheap HW band-limit); the agent applies pre-emphasis itself **before** its limiter → limiter is genuinely last → full deviation control incl. sibilants.
- **Simpler fallback:** SA818 pre-emphasis **ON** (correct HW curve), agent stays flat + band-limits + runs a conservative ceiling; the SA818 internal limiter is the sibilant backstop. Looser, but no agent-side pre-emphasis curve to get right.

**Open fact to confirm (not a blocker):** the SA818 power-on default filter state (datasheet / live `sa818 at` probe). The design works either way; this only sets the starting baseline.

---

## 6. Calibration procedure (one-time per board; runbook)

1. Inject a known reference (harness anchor **U**/**C**, −20 dBFS) with the DSP in the path; read **C/D** via the harness (headless, no keying).
2. On air (or with a deviation meter), A/B against a known-good reference station to find the limiter ceiling / input-gain that yields correct target hub (±2.5 kHz NBFM / ±5 kHz WBFM as applicable).
3. Use the **`sa818 at filters` debug shell** to try pre-emphasis on/off live during this step (before FilterCap lands) and pick the §5 configuration.
4. Record the resulting number into the Station calibration field (§4).

A short runbook doc captures this so a new board can be recalibrated reproducibly.

---

## 7. RF safety

- DSP, metering, filter-setting, and calibration measurement are **purely digital** and **never key the SA818**. Integrate with the existing RF interlock (refuse diagnostic/measurement actions while a real TX/PTT session is active, per #153/#154).
- The limiter is itself a deviation-safety element (splatter / good-neighbour), independent of audio quality.

---

## 8. Testing

- **Unit (`station-manager`):** gst arg builders for band-pass/gate/compressor/makeup/limiter params; calibration/config resolution + fallback + clamp; metering message schema; browser constraint change; graceful-degradation fallback path.
- **Unit (`FW-RemoteStation`):** FilterCap describe/execute (native_sim), AT formatting `AT+SETFILTER=p,h,l`, validation, working-state shadow; via the existing `SA818Simulator` + capability-system tests.
- **Integration (harness, on .211):** C/D deltas **with the DSP in path** → quantify gate/compressor/limiter behaviour objectively; confirm the gate does **not** clip real speech (feed a decaying tone/speech and verify tails survive at C/D).
- **On-station (human):** final hub calibration via on-air A/B; confirm word-ends present and no over-deviation.

---

## 9. Sequencing & conflict avoidance

- Branch the `station-manager` work **after PR #153 (merged) and #154 (anchor U)** land on main, so the agent DSP + metering build on the updated `engine.py` / `bridge_factory` / diagnostics plumbing and **reuse** the C/D taps. Avoids conflict in `station_agent/audio/engine.py` (the only overlap with #154).
- The three repo pieces are separable PRs: **(a)** `FW-RemoteStation` FilterCap (independent, mergeable first), **(b)** `linux-image` WirePlumber sink unity, **(c)** `station-manager` browser constraints + agent DSP + Station calibration field + TX meter. Per OE5XRX convention each is spec→plan→code on one feature branch, one PR.
- Browser-constraints change touches `static/js/audio-panel.js`, which the diagnostics harness work also edits — land after those merge or coordinate the diff.

---

## 10. Summary of changes

| Repo | Change |
|------|--------|
| `FW-RemoteStation` | New `FilterCap` (SA818 pre-emphasis/HPF/LPF) in `sa818_module.cpp` → `module describe`/`execute`. |
| `linux-image` | WirePlumber: FM TX sink default 0.40 → unity. |
| `station-manager` (browser) | `getUserMedia` NS/AGC/EC off + fixed capture `GainNode`. |
| `station-manager` (agent) | `build_tx_argv`: band-pass → gate → compressor → makeup → limiter (gst `audiodynamic`/`volume`); apply per-station calibration; graceful degradation. |
| `station-manager` (server) | `Station` calibration field (+migration) pushed via heartbeat; TX modulation meter over audio WS. |
| `station-manager` (UI) | `_audio_panel.html` live hub bar + "limiting" indicator. |
