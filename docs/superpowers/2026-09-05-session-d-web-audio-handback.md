# Session D — Web Audio Client + Mixer UI — Handback

**Date:** 2026-09-05
**Branch:** `feat/web-audio-client` (off `origin/main` @ `1c29bb9`)
**PR:** [#123](https://github.com/OE5XRX/station-manager/pull/123) (from `feat/web-audio-client`; **not merged** — awaiting human review)
**Scope:** Browser/frontend only. No server (Session C, #122, in main), no agent (Session B, #121, in main).
**Spec:** `docs/superpowers/specs/2026-09-03-audio-subsystem-design.md` — §5 (normative browser side), §3 (presets/local mix), §5.3 (frames), §5.4 (Opus), §5.6 (tx_route on control-plane).
**Plan:** `docs/superpowers/plans/2026-09-05-web-audio-client.md` (executed via subagent-driven-development, two-stage review per task + final whole-branch review).

---

## What was built

A browser Web-Audio client + mixer UI that lets an operator hear relayed Opus RX streams and transmit PTT-gated mic uplink, strictly per the §5 wire contract, integrated beside the existing control panel.

Architecture: all deterministic behavior lives in a pure, Node-tested UMD module; a thin Alpine shell owns the WebSocket / WebCodecs / WebAudio state and delegates every decision to it. PTT, lock and `tx_route` are reused from the control plane via a shared Alpine store — the mic is gated by the existing PTT state machine and `tx_route` travels on the lock-holding control WS (never the audio WS).

### Files (10 changed, +2681 / -1)

| File | New/Mod | Responsibility |
|------|---------|----------------|
| `static/js/audio-logic.js` | new (463) | Pure logic: §5.3 frame pack/parse (byte-identical to `station_agent/audio/frame.py`), `buildStreamIndex` (stream_ref↔stream_id), presets, mixer gain/mute, `dbToLinear`/`clampGainDb`, jitter buffer + wrap-aware seq-loss, `micWantsUplink`. UMD (`window.OE5XRXAudioLogic` / `module.exports`). |
| `tests/js/audio-logic.test.mjs` | new (156) | 21 Node assertions incl. the golden-fixture byte round-trip. |
| `tests/test_audio_logic_js.py` | new (34) | pytest wrapper running the Node suite in CI (mirrors `test_control_logic_js.py`). |
| `static/js/audio-panel.js` | new (1126) | Alpine `audioPanel`: audio WS (`/ws/audio/<id>/`), WebCodecs `AudioDecoder` per source → per-stream `GainNode` → master → destination, jitter buffer feed, mic capture (dedicated 16 kHz `AudioContext` → worklet → `AudioEncoder`) → PTT-gated uplink, sidetone, presets + localStorage. Delegates all logic to `audio-logic.js`. |
| `static/js/mic-worklet.js` | new (69) | `AudioWorkletProcessor` `oe5xrx-mic` buffering 20 ms chunks to the main thread. |
| `static/js/control-panel.js` | mod (+37) | Additive: registers `Alpine.store('control')` (`stationId,youHold,keyed,canControl,connected,txRoute,sendCommand`) and publishes live PTT/lock/connection state. No existing behavior changed. |
| `apps/control/templates/control/_audio_panel.html` | new (446) | Operator quickview (subscribe toggles + gain/mute + level meters + stream_state badges, preset selector, tx_route select, mic enable + sidetone) and a collapsible mixer matrix. |
| `apps/control/templates/control/panel.html` | mod (+6/-1) | Includes the audio partial as a sibling `x-data="audioPanel"` root; loads `audio-logic.js` then `audio-panel.js` with CSP nonces. |
| `config/settings/base.py` | mod (+1) | Adds `worker-src: [CSP.SELF]` (pins AudioWorklet + service worker to same-origin). |
| `tests/manual/audio-selftest.html` | new (344) | Standalone, self-contained WebCodecs decode proof (embeds the 291-byte golden fixture as base64 → `AudioDecoder(opus,8000,1)` → OfflineAudioContext FFT → asserts ~1 kHz peak). Human-runnable; deliberately **not** in the shipped static tree. |

---

## Key decisions

- **Target browsers:** modern browsers with native WebCodecs Opus — Chromium-based (Chrome/Edge ≥ 106) and Firefox ≥ 130. The client feature-detects `AudioDecoder`/`AudioEncoder` and shows an "unsupported" banner (no WS opened) elsewhere. **WASM Opus fallback is deferred (future work, §10)** — not built this session.
- **Pure-logic-first (like `control-logic.js`):** everything testable in Node lives in `audio-logic.js`; the WebAudio/WebCodecs shell is thin glue (no browser test harness / npm in this repo — JS unit tests run via `node` wrapped by pytest).
- **Reuse control plane for PTT/lock/tx_route:** a second control WS from the same user would not hold the lock, so `tx_route` (§5.6) and mic-gating go through the existing control connection via `Alpine.store('control')`. The audio panel never sends `ptt`/`lock` and never opens a second control WS.
- **`tx_route` addressing:** command sent on the control WS as `{type:"command", request_id, slot:1000, module:"audio-router", capability:"tx_route", op:"set", value:{slot,module}|null}` — the router's synthetic address (`station_agent/audio/router_module.py`: slot 1000, `MODULE_ID="audio-router"`), lock-gated server-side.
- **Mic at 16 kHz:** WebCodecs `AudioEncoder` does not resample, so the mic uses a dedicated `AudioContext({sampleRate:16000})`; the RX decode graph stays on the main context (decoded 8 kHz buffers resample on connect). Encoder configured once via `isConfigSupported` (VOIP + in-band FEC where supported).
- **PLC on loss (§5.4):** loss is detected via `seq` in a pure jitter buffer; a lost frame is skipped at decode (Opus PLC/in-band FEC conceals). WebCodecs `AudioDecoder` has no explicit PLC-inject API — skip-chunk is the MVP behavior (see ruling below). `flags` bit0 is treated as advisory and never gates FEC.
- **Sidetone** is a browser-local monitor built on the main playback context from the shared `MediaStream` (a second `MediaStreamAudioSourceNode` — Web Audio nodes cannot connect across contexts). Off by default.

---

## Test outputs / evidence

```
$ node tests/js/audio-logic.test.mjs
… 21 assertions passed

$ .venv/bin/python -m pytest tests/test_audio_logic_js.py tests/test_control_logic_js.py -q
..  2 passed

$ .venv/bin/python manage.py check
System check identified no issues (1 silenced).
```

**Frame byte-identity (probe, Python ↔ JS on the golden fixture `tests/fixtures/audio/media_frame_slot0rx.bin`):**
```
Python parse_frame : stream_ref=0 seq=0 ts=0 flags=0 reserved=0 payload_len=279   header=a50100000000000000000000  round-trip==fixture: True
Node   parseFrame  : stream_ref=0 seq=0 ts=0 flags=0 reserved=0 payload_len=279   header=a50100000000000000000000  round-trip==fixture: true
```

**Review evidence (subagent-driven, per plan):**
- Every task: two-stage review (atlas spec-compliance → audit quality). The Tasks 6+7 pass caught real bugs before merge — `tx_route` mis-addressing, a deprecated/leaky ScriptProcessor fallback, an encoder configure race, a 16 kHz-vs-context sample-rate mismatch, and GainNode/decoder lifecycle leaks — all fixed and re-reviewed.
- Final whole-branch review (opus): **SHIP-WITH-MINORS**. Security (guard): **PASS-WITH-NOTES**. Contract (probe): **CONTRACT-ALIGNED**.

---

## Manual verification checklist (human, in a WebCodecs browser)

Runtime audio behavior cannot be exercised headless (no WebCodecs/WebAudio in Node/CI). Please verify:

1. **Decode proof:** open `tests/manual/audio-selftest.html` in Chrome/Edge ≥ 106 or Firefox ≥ 130 → shows `PASS` with peak ≈ 1000 Hz.
2. **Connect + streams:** on a station control page, the audio panel connects (`conn=open`); `streams` populate; the **FM** preset auto-subscribes `slot0.rx` + `op.mic`.
3. **Mix:** gain slider and mute change the audible level; the level meter tracks; stream_state badge shows live/idle.
4. **Satellite preset:** drops `op.mic` (RX-only), no double audio.
5. **Mic uplink:** acquire the lock and hold PTT (control panel) → mic uplink frames flow **only** while keyed; releasing PTT stops them. Without the lock, a single `error{not_locked}` notice appears (not spammed per frame).
6. **tx_route:** the select issues a control-WS command (verify in devtools → WS frames: `slot:1000, module:"audio-router", capability:"tx_route"`); it is rejected server-side when not holding the lock.
7. **Sidetone:** toggling monitors your own mic locally; the gain slider updates the monitor live.
8. **Mic teardown / privacy:** disabling the mic (or navigating away) stops the mic track (no hot mic).
9. **Unsupported browser:** a non-WebCodecs browser shows the unsupported banner and does not crash or open the WS.

---

## Open items / deferred (with rulings)

**Recommended before/after merge (human decision):**
- **CSP `connect-src` tightening (guard, Medium):** currently `[SELF, "ws:", "wss:"]` app-wide. Tightening to `[CSP.SELF]` removes a WebSocket-exfil vector (only reachable via a pre-existing XSS). **Deferred** because it is an app-wide CSP change affecting the existing control WS and cannot be browser-verified in this headless session — recommend a human applies it and confirms both the control and audio WebSockets still connect in the target browsers. `worker-src: [CSP.SELF]` was added this session (safe, additive).

**Deferred cosmetics / low-impact (rulings in the SDD ledger):**
- **`_cssEscape` doesn't escape `[` (control-panel.js, Low):** pre-existing, not introduced here; non-exploitable (module/capability IDs are regex-constrained, no `[`).
- **tx_route result not client-tracked (probe F1, Minor):** the happy path works; the only effect is a harmless unnecessary 10 s server-side timeout task and a silently-dropped stray result frame. Fixing would add churn to `control-panel.js`.
- **Always-on sidetone monitor (Minor):** sidetone monitors the mic regardless of keying; defensible per §3 ("browser-local monitor, optional") and user-toggled. Could be gated on keying if desired.
- **Level meter freezes at last value on stream stop; brief 0 dB mixer flash before the first `streams` message; tx_route option `JSON.stringify`/`parse` round-trip** — cosmetic; the tx_route parse is now wrapped in try/catch.

**Future work (out of scope, per spec):**
- WASM Opus decoder fallback for non-WebCodecs browsers (§10).
- The full "browser hears real relayed audio across all layers" E2E is **Session E** (this session delivered client logic + Node fixture cross-checks + a headless-decode self-test page).

---

## Ruling on PLC (from the SDD ledger)

WebCodecs `AudioDecoder` has no explicit PLC-inject API; a dropped chunk conceals as a gap and Opus in-band FEC recovers at decode when the next packet carries it. Full PLC fidelity is a decoder-internal concern. Decision: skip-chunk-on-loss is the MVP behavior; cost if wrong is a rework of the decode loop only, isolated to `audio-panel.js`.
