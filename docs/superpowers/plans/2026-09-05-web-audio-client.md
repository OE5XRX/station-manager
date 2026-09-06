# Web Audio Client + Mixer UI (Session D) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a browser operator a Web-Audio client + mixer UI that hears relayed Opus RX streams and sends PTT-gated mic uplink, strictly per the §5 wire contract, integrated beside the existing control panel.

**Architecture:** All deterministic behavior lives in a pure, Node-testable UMD module `static/js/audio-logic.js` (mirrors `control-logic.js`): §5.3 frame pack/parse (byte-identical to `station_agent/audio/frame.py`), `stream_ref↔stream_id` mapping, subscription presets, mixer gain/mute state, jitter-buffer/seq-loss logic, and the mic-uplink coupling predicate. A thin stateful Alpine shell `static/js/audio-panel.js` owns the audio WebSocket, the WebCodecs `AudioDecoder`/`AudioEncoder` and the WebAudio graph — it delegates every decision to `audio-logic.js`. PTT, lock and `tx_route` are **reused from the control plane**: the audio shell reads PTT/lock state and sends `tx_route` through a shared `Alpine.store('control')` that the control panel publishes, so the mic is gated by the existing PTT state machine and `tx_route` travels on the lock-holding control WebSocket (never the audio WS).

**Tech Stack:** Vanilla ES5-ish UMD JS (no build step, no npm), Node's built-in test runner (`node:assert`) invoked via pytest, Alpine.js, WebCodecs (Opus), WebAudio (`AudioContext`, `GainNode`, `AudioWorklet`), Bootstrap 5, Django templates.

**Spec:** `docs/superpowers/specs/2026-09-03-audio-subsystem-design.md` — §5 (NORMATIVE, browser side), §3 (browser-local mix + presets), §5.3 (binary media frames), §5.4 (Opus profile), §5.6 (`tx_route` on control-plane). Handbacks: `docs/superpowers/2026-09-05-session-b-audio-agent-handback.md`, `docs/superpowers/2026-09-05-session-c-audio-server-handback.md`.

## Global Constraints

- **Wire contract is §5, verbatim.** Browser→server JSON: `hello`, `subscribe`, `unsubscribe`, `mic_open`, `mic_close`. Server→browser JSON: `streams`, `stream_state`, `error`. Every JSON frame carries `"v": 1`. Endpoint `wss://<host>/ws/audio/<station_id>/`, same-origin session-cookie auth. **No subscribe-ack exists** — `stream_ref` is read ONLY from the `streams` message entries, never inferred from array index (§5.2 amendment).
- **§5.3 frame layout (little-endian), byte-identical to `frame.py`:** offset 0 `u8 magic=0xA5`, 1 `u8 ver=1`, 2 `u16 stream_ref`, 4 `u16 seq`, 6 `u32 ts`, 10 `u8 flags` (bit0=FEC, bit1=DTX, bit2=marker), 11 `u8 reserved=0`, 12.. Opus payload. `stream_ref`/`seq` mask to 16 bits, `ts` to 32 bits (wrap by design).
- **`flags` bit0 (FEC) is advisory (§5.3/§5.4).** The receiver MUST NOT gate FEC handling on bit0. Decode always with in-band FEC + PLC on; loss is detected via `seq`.
- **Opus profile (§5.4):** 20 ms frames, VBR, VOIP, in-band FEC on, PLC on at decode, DTX allowed. RX (module) = 8 kHz NB mono; op.mic uplink = 16 kHz WB mono (per `advertise` `format`).
- **`tx_route` is a control-plane command (§5.6), NOT on the audio WS.** Shape sent on `/ws/control/<station_id>/`: `{type:"command", request_id, slot:1000, module:"audio-router", capability:"tx_route", op:"set", value:{slot,module}}` (or `value:null` to clear). Lock-gated server-side. Router module id = `audio-router`, synthetic slot `1000` (`station_agent/audio/router_module.py`).
- **Mic uplink self-gating:** the browser sends mic binary frames ONLY while the operator holds the lock AND PTT is keyed for a module. Otherwise the server drops them and replies `error{code:"not_locked"}`. The browser must not spam frames when un-keyed.
- **Presets (§3):** *FM* = subscribe module-RX sources **and** `op.mic`. *Satellite/full-duplex* = subscribe module-RX (downlink) sources **only, no `op.mic`** (avoids double audio). *Custom* = explicit per-source toggles. Sidetone = browser-local monitor of own mic, optional. Presets are per-user, browser-local (localStorage).
- **WebCodecs target (§10):** modern browsers with native WebCodecs Opus (current Chromium/Firefox). No WASM fallback now — feature-detect and show a clear unsupported message; document WASM as future work.
- **No npm/build step.** JS unit tests are `tests/js/audio-logic.test.mjs`, run with `node`, wrapped by `tests/test_audio_logic_js.py` (mirror `tests/test_control_logic_js.py`). Pure logic in a UMD module usable via `require()` from Node and `window.OE5XRXAudioLogic` in the browser.
- **Django templates:** multi-line `{# … #}` is FORBIDDEN — use `{% comment %} … {% endcomment %}`. CSP: JS loads via `<script src>` with nonce, no inline logic.
- **DE-locale number inputs:** any numeric `<input>` (gain sliders/fields) forces dot-decimal (`lang="en"`); reuse `OE5XRXControlLogic.parseNumber`/`formatNumber` where a value is typed.
- **Style:** UMD/logic files follow `static/js/app.js` conventions (`"use strict"`, `var`/`function`, ES5-ish). The Alpine object uses modern method syntax like `control-panel.js`.

---

### Task 1: Pure logic — §5.3 frame codec + fixture round-trip

**Files:**
- Create: `static/js/audio-logic.js`
- Test: `tests/js/audio-logic.test.mjs`

**Interfaces:**
- Consumes: nothing.
- Produces (browser: `window.OE5XRXAudioLogic`, Node: `module.exports`):
  - `MAGIC=0xA5`, `VERSION=1`, `HEADER_LEN=12`, `FLAG_FEC=0x01`, `FLAG_DTX=0x02`, `FLAG_MARKER=0x04`, `AUDIO_PROTOCOL_VERSION=1`.
  - `packFrame({stream_ref, seq, ts, flags, payload})` → `Uint8Array` (`payload` is `Uint8Array`/array-like of bytes; `flags` defaults 0).
  - `parseFrame(bytes)` → `{stream_ref, seq, ts, flags, reserved, payload: Uint8Array, fec, dtx, marker}` or throws `FrameError` (bad magic/version/too short). `bytes` is `Uint8Array`/`ArrayBuffer`.
  - `FrameError` (Error subclass).

- [ ] **Step 1: Write the failing test**

```js
// tests/js/audio-logic.test.mjs (header mirrors control-logic.test.mjs)
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const A = require(path.resolve(here, "../../static/js/audio-logic.js"));
const FIXTURE = path.resolve(here, "../fixtures/audio/media_frame_slot0rx.bin");

let passed = 0;
function ok(name, fn) { fn(); passed += 1; console.log("ok - " + name); }

// --- §5.3 frame codec: fixture cross-check against frame.py ---------------
ok("parseFrame parses the golden fixture header", () => {
  const bytes = new Uint8Array(readFileSync(FIXTURE));
  const f = A.parseFrame(bytes);
  assert.equal(f.stream_ref, 0);
  assert.equal(f.seq, 0);
  assert.equal(f.ts, 0);
  assert.equal(f.flags, 0);
  assert.equal(f.payload.length, bytes.length - 12); // 279
  assert.equal(f.payload[0], 0x98); // first opus byte
});
ok("pack(parse(fixture)) is byte-identical", () => {
  const bytes = new Uint8Array(readFileSync(FIXTURE));
  const f = A.parseFrame(bytes);
  const out = A.packFrame(f);
  assert.deepEqual(Array.from(out), Array.from(bytes));
});
ok("packFrame writes the exact little-endian header", () => {
  const out = A.packFrame({ stream_ref: 0x0102, seq: 0x0304, ts: 0x05060708, flags: 0x05, payload: new Uint8Array([0xAA]) });
  // magic, ver, ref LE, seq LE, ts LE, flags, reserved, payload
  assert.deepEqual(Array.from(out), [0xA5,0x01, 0x02,0x01, 0x04,0x03, 0x08,0x07,0x06,0x05, 0x05, 0x00, 0xAA]);
});
ok("stream_ref/seq wrap at 2^16, ts at 2^32", () => {
  const out = A.packFrame({ stream_ref: 0x1FFFF, seq: 0x10000, ts: 0x1FFFFFFFF, flags: 0, payload: new Uint8Array() });
  const f = A.parseFrame(out);
  assert.equal(f.stream_ref, 0xFFFF);
  assert.equal(f.seq, 0);
  assert.equal(f.ts, 0xFFFFFFFF);
});
ok("flags predicates", () => {
  const f = A.parseFrame(A.packFrame({ stream_ref:1, seq:1, ts:1, flags: A.FLAG_FEC|A.FLAG_MARKER, payload:new Uint8Array() }));
  assert.equal(f.fec, true); assert.equal(f.dtx, false); assert.equal(f.marker, true);
});
ok("parseFrame rejects bad magic / short / bad version", () => {
  assert.throws(() => A.parseFrame(new Uint8Array(11)), A.FrameError);
  const bad = A.packFrame({ stream_ref:0, seq:0, ts:0, flags:0, payload:new Uint8Array() }); bad[0] = 0x00;
  assert.throws(() => A.parseFrame(bad), A.FrameError);
  const badv = A.packFrame({ stream_ref:0, seq:0, ts:0, flags:0, payload:new Uint8Array() }); badv[1] = 2;
  assert.throws(() => A.parseFrame(badv), A.FrameError);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/js/audio-logic.test.mjs`
Expected: FAIL (`Cannot find module .../audio-logic.js`).

- [ ] **Step 3: Implement the codec in `static/js/audio-logic.js`**

Create the UMD wrapper (copy the exact `(function(root, factory){...})` shape from `control-logic.js`, export name `OE5XRXAudioLogic`). Implement `packFrame`/`parseFrame` using `DataView` (LE) — do NOT depend on Node Buffer (must run in the browser). `packFrame` returns a `Uint8Array` of `12 + payload.length`; write magic/ver/reserved as bytes, `stream_ref`&`seq` masked with `& 0xFFFF`, `ts` masked `>>>0`, then copy payload bytes at offset 12. `parseFrame` reads via `DataView`, validates `magic===0xA5`/`ver===1`/`length>=12`, returns the object with `fec/dtx/marker` booleans and `payload` as a `Uint8Array` slice (copy, not view). `FrameError extends Error`.

- [ ] **Step 4: Run to verify it passes**

Run: `node tests/js/audio-logic.test.mjs`
Expected: all `ok - ...` lines, exit 0.

- [ ] **Step 5: Commit**

```bash
git add static/js/audio-logic.js tests/js/audio-logic.test.mjs
git commit -m "feat(audio-web): §5.3 frame codec, byte-identical to frame.py"
```

---

### Task 2: Pure logic — stream map, presets, mixer state, uplink coupling

**Files:**
- Modify: `static/js/audio-logic.js`
- Test: `tests/js/audio-logic.test.mjs` (append)

**Interfaces:**
- Consumes: Task 1 exports.
- Produces:
  - `buildStreamIndex(streamsMsg)` → `{byId:{[stream_id]:entry}, byRef:{[stream_ref]:stream_id}, list:[entry]}`. `entry` = the raw `streams[]` object (`stream_id, slot, module, direction, format, codec, stream_ref`). Ignores entries missing `stream_id`/`stream_ref`.
  - `isOpMic(entry)` → bool (`entry.module === "operator"` or `entry.stream_id === "op.mic"`).
  - `isRxSource(entry)` → bool (`entry.direction === "rx"` and not op.mic).
  - `presetSubscriptions(preset, list)` → sorted `string[]` of `stream_id`s. `preset ∈ {"fm","satellite","custom"}`. `fm` = all RX sources + op.mic. `satellite` = all RX sources, NO op.mic. `custom` returns `[]` (caller supplies explicit set).
  - `PRESETS = ["fm","satellite","custom"]`.
  - Mixer: `defaultMixerEntry()` → `{gainDb:0, muted:false}`. `dbToLinear(db)` → number (`10^(db/20)`, `db<=-60 ⇒ 0`). `clampGainDb(db)` → number clamped to `[-60, 12]` (uses `OE5XRXControlLogic.parseNumber` semantics: accepts comma/dot; `null`→`0`). `effectiveGain(entry)` → linear gain, `0` when `muted`.
  - Uplink coupling: `micWantsUplink({micEnabled, keyed, youHold})` → bool = `micEnabled && keyed && youHold`.

- [ ] **Step 1: Write the failing tests (append)**

```js
// --- stream index + presets ------------------------------------------------
const STREAMS = { v:1, type:"streams", streams:[
  { stream_id:"slot0.rx", slot:0, module:"fm", direction:"rx", format:{rate:8000,channels:1}, codec:"opus", stream_ref:0 },
  { stream_id:"op.mic", slot:null, module:"operator", direction:"rx", format:{rate:16000,channels:1}, codec:"opus", stream_ref:1 },
]};
ok("buildStreamIndex maps ref<->id both ways", () => {
  const ix = A.buildStreamIndex(STREAMS);
  assert.equal(ix.byRef[0], "slot0.rx");
  assert.equal(ix.byRef[1], "op.mic");
  assert.equal(ix.byId["slot0.rx"].stream_ref, 0);
  assert.equal(ix.list.length, 2);
});
ok("buildStreamIndex ignores entries without id/ref", () => {
  const ix = A.buildStreamIndex({ streams:[{ stream_id:"x" }, { stream_ref:5 }] });
  assert.equal(ix.list.length, 0);
});
ok("fm preset = rx sources + op.mic", () => {
  assert.deepEqual(A.presetSubscriptions("fm", STREAMS.streams), ["op.mic","slot0.rx"]);
});
ok("satellite preset = rx sources only, no op.mic", () => {
  assert.deepEqual(A.presetSubscriptions("satellite", STREAMS.streams), ["slot0.rx"]);
});
ok("custom preset = empty (explicit set supplied by caller)", () => {
  assert.deepEqual(A.presetSubscriptions("custom", STREAMS.streams), []);
});
// --- mixer -----------------------------------------------------------------
ok("dbToLinear: 0dB=1, -6dB≈0.501, floor to 0", () => {
  assert.equal(A.dbToLinear(0), 1);
  assert.ok(Math.abs(A.dbToLinear(-6) - 0.5012) < 1e-3);
  assert.equal(A.dbToLinear(-60), 0);
});
ok("clampGainDb clamps and parses comma decimal", () => {
  assert.equal(A.clampGainDb("3,5"), 3.5);
  assert.equal(A.clampGainDb(99), 12);
  assert.equal(A.clampGainDb(-999), -60);
  assert.equal(A.clampGainDb("abc"), 0);
});
ok("effectiveGain 0 when muted", () => {
  assert.equal(A.effectiveGain({ gainDb:0, muted:true }), 0);
  assert.equal(A.effectiveGain({ gainDb:0, muted:false }), 1);
});
// --- uplink coupling -------------------------------------------------------
ok("micWantsUplink requires micEnabled AND keyed AND youHold", () => {
  assert.equal(A.micWantsUplink({ micEnabled:true, keyed:true, youHold:true }), true);
  assert.equal(A.micWantsUplink({ micEnabled:true, keyed:false, youHold:true }), false);
  assert.equal(A.micWantsUplink({ micEnabled:true, keyed:true, youHold:false }), false);
  assert.equal(A.micWantsUplink({ micEnabled:false, keyed:true, youHold:true }), false);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/js/audio-logic.test.mjs`
Expected: FAIL (`A.buildStreamIndex is not a function`).

- [ ] **Step 3: Implement**

Add the functions to `audio-logic.js`. For `clampGainDb`, reuse the DE-locale parse: if `window.OE5XRXControlLogic` exists use its `parseNumber`, else inline the same comma→dot logic (the module must not hard-require control-logic in Node — inline a local `parseNumber` fallback). `presetSubscriptions` sorts the result with `.sort()` for determinism. Export all new names in the return object.

- [ ] **Step 4: Run to verify it passes**

Run: `node tests/js/audio-logic.test.mjs`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add static/js/audio-logic.js tests/js/audio-logic.test.mjs
git commit -m "feat(audio-web): stream index, presets, mixer, uplink coupling logic"
```

---

### Task 3: Pure logic — jitter buffer + seq-loss detection

**Files:**
- Modify: `static/js/audio-logic.js`
- Test: `tests/js/audio-logic.test.mjs` (append)

**Interfaces:**
- Consumes: Task 1/2.
- Produces:
  - `seqDelta(prev, next)` → signed number of the shortest u16 path from `prev` to `next` (`+1` in-order, `0` duplicate, `-1` one late/reorder). Wrap-aware: `seqDelta(65535, 0) === 1`, `seqDelta(0, 65535) === -1`.
  - `createJitter({depth})` → state object `{depth, frames:{}, next:null, started:false}` (`depth` = max reorder window in frames, default 3).
  - `jitterPush(state, {seq, frame})` → mutates state; drops duplicates/too-late frames. Returns `{accepted:bool}`.
  - `jitterDrain(state)` → `{out:[{seq, frame, plc:false}...], gaps:[{seq, count}]}` — pops in-order ready frames once buffered depth is reached; when the head is missing but later frames exist beyond `depth`, emits a `plc:true` placeholder (`frame:null`) for the missing `seq` and advances (loss → PLC/FEC at decode time). Deterministic; no time input.

- [ ] **Step 1: Write the failing tests (append)**

```js
// --- seq math (u16 wrap) ---------------------------------------------------
ok("seqDelta wrap-aware", () => {
  assert.equal(A.seqDelta(10, 11), 1);
  assert.equal(A.seqDelta(10, 10), 0);
  assert.equal(A.seqDelta(11, 10), -1);
  assert.equal(A.seqDelta(65535, 0), 1);
  assert.equal(A.seqDelta(0, 65535), -1);
});
// --- jitter buffer ---------------------------------------------------------
function drainSeqs(res) { return res.out.map(o => (o.plc ? "P" : o.seq)); }
ok("in-order frames drain in order after depth fills", () => {
  const s = A.createJitter({ depth: 2 });
  A.jitterPush(s, { seq: 0, frame: "a" });
  A.jitterPush(s, { seq: 1, frame: "b" });
  A.jitterPush(s, { seq: 2, frame: "c" });
  const r = A.jitterDrain(s);
  assert.deepEqual(drainSeqs(r), [0, 1]); // head-of-line released, depth kept buffered
});
ok("reordered-within-depth frames are sorted", () => {
  const s = A.createJitter({ depth: 3 });
  A.jitterPush(s, { seq: 0, frame: "a" });
  A.jitterPush(s, { seq: 2, frame: "c" });
  A.jitterPush(s, { seq: 1, frame: "b" });
  A.jitterPush(s, { seq: 3, frame: "d" });
  assert.deepEqual(drainSeqs(A.jitterDrain(s)), [0, 1]);
});
ok("a lost frame becomes a PLC placeholder once depth is exceeded", () => {
  const s = A.createJitter({ depth: 1 });
  A.jitterPush(s, { seq: 0, frame: "a" });
  A.jitterPush(s, { seq: 2, frame: "c" }); // seq 1 lost
  A.jitterPush(s, { seq: 3, frame: "d" });
  const r = A.jitterDrain(s);
  assert.deepEqual(drainSeqs(r), [0, "P", 2]);
  assert.deepEqual(r.gaps, [{ seq: 1, count: 1 }]);
});
ok("duplicates and too-late frames are dropped", () => {
  const s = A.createJitter({ depth: 2 });
  A.jitterPush(s, { seq: 5, frame: "a" });
  A.jitterDrain(s);
  assert.equal(A.jitterPush(s, { seq: 5, frame: "dup" }).accepted, false);
  assert.equal(A.jitterPush(s, { seq: 3, frame: "late" }).accepted, false);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `node tests/js/audio-logic.test.mjs`
Expected: FAIL (`A.seqDelta is not a function`).

- [ ] **Step 3: Implement**

`seqDelta(prev,next)`: `d = ((next - prev) & 0xFFFF); return d > 0x8000 ? d - 0x10000 : d;` (0x8000 maps to -32768 fine). `createJitter`: store frames in a map keyed by seq. `jitterPush`: compute `seqDelta(state.next-1, seq)` relative to the expected head; drop if already-emitted (`state.next!=null && seqDelta(seq, state.next) >= 0` i.e. seq < next) or a duplicate in the map. `jitterDrain`: initialize `state.next` to the lowest buffered seq on first drain; while the buffered span (`highest - next`) exceeds `depth`, if `frames[next]` exists emit it and delete, else emit a `{plc:true, seq:next, frame:null}` placeholder and record a gap `{seq:next,count:1}` (coalesce consecutive gaps into `count`); advance `next` (u16 wrap via `& 0xFFFF`). Keep exactly `depth` most-recent frames buffered. All arithmetic u16-wrapped.

- [ ] **Step 4: Run to verify it passes**

Run: `node tests/js/audio-logic.test.mjs`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add static/js/audio-logic.js tests/js/audio-logic.test.mjs
git commit -m "feat(audio-web): jitter buffer + wrap-aware seq-loss detection"
```

---

### Task 4: pytest wrapper for the JS suite

**Files:**
- Create: `tests/test_audio_logic_js.py`

**Interfaces:**
- Consumes: `tests/js/audio-logic.test.mjs`.
- Produces: a pytest that runs the Node suite in CI (skips if `node` absent).

- [ ] **Step 1: Write the test** (copy `tests/test_control_logic_js.py` verbatim, swap the filename)

```python
"""Runs the Node pure-logic suite for static/js/audio-logic.js.

Skips if node is not on PATH. Node IS installed in CI, so this must run and
pass there — the audio wire logic (§5.3 frame codec, presets, mixer, jitter/
seq-loss) is the correctness foundation the Alpine audio component relies on
and can only be exercised outside a browser via Node.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = REPO_ROOT / "tests" / "js" / "audio-logic.test.mjs"


def test_audio_logic_js():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH — JS pure-logic suite skipped")
    assert TEST_FILE.exists(), f"missing {TEST_FILE}"
    result = subprocess.run(
        [node, str(TEST_FILE)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"audio-logic.test.mjs failed (exit {result.returncode})\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
```

- [ ] **Step 2: Run to verify it passes**

Run: `python -m pytest tests/test_audio_logic_js.py -v`
Expected: PASS (or SKIP only if node missing; node v20 is present).

- [ ] **Step 3: Commit**

```bash
git add tests/test_audio_logic_js.py
git commit -m "test(audio-web): pytest wrapper for the audio-logic Node suite"
```

---

### Task 5: Shared control store — publish PTT/lock + `sendControl` bridge

**Files:**
- Modify: `static/js/control-panel.js`
- Test: manual (Alpine store is browser-only glue; the pure predicate it feeds is already covered by `micWantsUplink` in Task 2). Add a regression note in the manual checklist (Task 9).

**Interfaces:**
- Consumes: existing `controlPanel` Alpine component internals (`_send`, `youHold`, `canControl`, `ptt`, `stationId`, `_anyKeyed`).
- Produces: `Alpine.store('control')` with reactive fields the audio panel reads:
  - `stationId` (string), `youHold` (bool), `keyed` (bool = any module keyed), `canControl` (bool), `connected` (bool).
  - `sendCommand(obj)` → sends `obj` on the control WS with the §7 envelope (returns bool), used by the audio panel for `tx_route`. This is the ONLY control-WS write the audio panel performs.
  - `txRoute` ({slot,module}|null) — last tx_route the operator selected (mirrored for UI display).

- [ ] **Step 1: Register the store on `alpine:init`** (additive; keep existing behavior intact)

In `control-panel.js`, inside the existing `document.addEventListener("alpine:init", ...)` block, register:

```js
window.Alpine.store("control", {
  stationId: null,
  youHold: false,
  keyed: false,
  canControl: false,
  connected: false,
  txRoute: null,
  _send: null, // set by the controlPanel init
  sendCommand: function (obj) {
    return typeof this._send === "function" ? this._send(obj) : false;
  },
});
```

- [ ] **Step 2: Publish state from the component (additive lines only)**

- In `init:` after `this.stationId = ...`, add: `var store = window.Alpine.store("control"); store.stationId = this.stationId; store._send = this._send.bind(this);`
- Add a private helper on the component:

```js
_publishControlStore: function () {
  var s = window.Alpine.store && window.Alpine.store("control");
  if (!s) return;
  s.youHold = this.youHold;
  s.keyed = this._anyKeyed();
  s.canControl = this.canControl;
  s.connected = this.conn === "open" && !this.agentOffline;
},
```

- Call `this._publishControlStore();` at the end of: `_onLock`, `_onState` (after PTT phase update), `pttDown`, `_unkeyModule`, `_ingestInventory`, and in the WS `open`/`close` handlers (`self._publishControlStore()`), so `keyed`/`youHold`/`canControl`/`connected` stay live.
- In `_sendCommand`, when `cap === "tx_route"` is sent, also mirror the selected value into `s.txRoute` (the audio panel calls `sendCommand` directly, so this is optional; keep the store field for display).

- [ ] **Step 3: Verify existing control tests still pass**

Run: `python -m pytest tests/test_control_logic_js.py -v` (pure logic untouched → PASS). Manually confirm in a browser (Task 9 checklist) that PTT/lock still work and the store fields update (log `Alpine.store('control')` in devtools).

- [ ] **Step 4: Commit**

```bash
git add static/js/control-panel.js
git commit -m "feat(audio-web): publish PTT/lock state + sendCommand bridge via Alpine store"
```

---

### Task 6: Audio WebSocket + WebCodecs decode + WebAudio mix shell

**Files:**
- Create: `static/js/audio-panel.js` (Alpine component `audioPanel`)
- Test: browser self-test page (Task 8) + manual checklist (Task 9). Logic is delegated to `audio-logic.js` (already tested); this file is thin glue.

**Interfaces:**
- Consumes: `window.OE5XRXAudioLogic` (Task 1–3), `Alpine.store('control')` (Task 5).
- Produces: Alpine component `audioPanel` with reactive state the template reads:
  - `conn` ('connecting'|'open'|'closed'), `supported` (bool WebCodecs feature-detect), `unsupportedReason` (string), `streams` (array of index entries), `subs` ({[stream_id]:bool}), `mixer` ({[stream_id]:{gainDb,muted}}), `preset` ('fm'|'satellite'|'custom'), `micEnabled` (bool), `sidetone` (bool), `sidetoneGainDb` (number), `txRoute` ({slot,module}|null), `levels` ({[stream_id]:number} 0..1 for meters).
  - Methods: `init`, `destroy`, `toggleSub(stream_id)`, `applyPreset(name)`, `setGain(stream_id, raw)`, `toggleMute(stream_id)`, `enableMic()`, `disableMic()`, `toggleSidetone()`, `setTxRoute(slot, module)` (calls `store.sendCommand`), `isSubscribed(stream_id)`.

- [ ] **Step 1: WebCodecs feature-detect + WS lifecycle**

Implement `init`: read `data-station-id` from `$el`; `this.supported = typeof window.AudioDecoder === "function" && typeof window.AudioEncoder === "function"`; if unsupported set `unsupportedReason` and return early (render banner, no WS). Else create `AudioContext`, connect a master `GainNode` → `destination`, load `mic-worklet.js` via `audioCtx.audioWorklet.addModule(...)` (deferred until mic enable). Open the audio WS to `proto//host/ws/audio/<stationId>/` (mirror `control-panel.js` `_wsUrl`/`_connect`/`_scheduleReconnect` with backoff; permanent-deny close codes 4401/4403/4404 stop retry and set `unsupportedReason`/terminal state). On open: send `hello` `{v:1,type:"hello",codecs:["opus"],webcodecs:true}` and re-apply the saved preset's subscriptions.

- [ ] **Step 2: Handle server→browser JSON + binary**

`onmessage`: if `ev.data instanceof ArrayBuffer`/Blob → `A.parseFrame` → route to the per-`stream_ref` decoder path (Step 3). Else `JSON.parse`; switch `msg.type`:
- `"streams"`: `this._index = A.buildStreamIndex(msg)`; set `this.streams = this._index.list`; ensure a `mixer` entry (`A.defaultMixerEntry()`) exists per stream; re-send `subscribe` for any `subs` still desired (ref may have changed); tear down decoders for streams that disappeared.
- `"stream_state"`: update a per-stream `state` map (`live`/`idle`/`error`) for the UI badge.
- `"error"`: if `code==="not_locked"` surface a transient "PTT/lock required to transmit" notice (do NOT spam); other codes → console + small UI notice.

Binary requests the WS with `ws.binaryType = "arraybuffer"`.

- [ ] **Step 3: Per-source decode graph**

For each subscribed source, lazily create: an `AudioDecoder` configured `{codec:"opus", sampleRate: entry.format.rate, numberOfChannels: entry.format.channels}` with `output` callback that turns the `AudioData` into an `AudioBufferSourceNode` scheduled on a per-stream timeline → a per-stream `GainNode` (`gain.value = A.effectiveGain(mixer[stream_id])`) → master gain. Feed decode from a per-stream jitter buffer: on each media frame `A.jitterPush(jitter[ref], {seq, frame})`, then `A.jitterDrain(...)`; for each `out` item decode `EncodedAudioChunk({type:"key", timestamp, data: frame.payload})`; for `plc:true` items, skip data (Opus PLC is inherent — a missing chunk conceals). Update `levels[stream_id]` from a cheap RMS/analyser for the meter. `setGain`/`toggleMute` update `mixer` (via `A.clampGainDb`) and set the live `GainNode.gain`.

- [ ] **Step 4: Subscribe/preset control**

`toggleSub(id)`: flip `subs[id]`, send `{v:1,type:"subscribe",stream_ids:[id]}` or `unsubscribe`, create/tear down its decoder. `applyPreset(name)`: `this.preset=name`; `const want=A.presetSubscriptions(name, this.streams)`; diff against current `subs`, send subscribe/unsubscribe deltas; persist `preset` + custom `subs` to `localStorage` (`oe5xrx.audio.<stationId>`). On `init`, load persisted preset/subs.

- [ ] **Step 5: Commit**

```bash
git add static/js/audio-panel.js
git commit -m "feat(audio-web): audio WS client + WebCodecs decode + WebAudio mix"
```

---

### Task 7: Mic capture → encode → PTT-gated uplink + sidetone

**Files:**
- Create: `static/js/mic-worklet.js` (AudioWorklet processor)
- Modify: `static/js/audio-panel.js`
- Test: browser self-test (Task 8) + manual checklist (Task 9).

**Interfaces:**
- Consumes: Task 6 shell, `A.packFrame`, `A.micWantsUplink`, `Alpine.store('control')`.
- Produces: mic pipeline: `enableMic()`, `disableMic()`, `toggleSidetone()`, an internal `_pumpUplink()` gated by the control store.

- [ ] **Step 1: mic-worklet.js**

An `AudioWorkletProcessor` (`class MicProcessor extends AudioWorkletProcessor`) that buffers input frames into 20 ms chunks at the context rate and `port.postMessage`s `Float32Array` chunks to the main thread. Register with `registerProcessor("oe5xrx-mic", MicProcessor)`. No Opus here — encoding happens on the main thread with `AudioEncoder`.

- [ ] **Step 2: enableMic()**

`getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true}})`; create a `MediaStreamAudioSourceNode` → the `oe5xrx-mic` worklet node. Create an `AudioEncoder` `{codec:"opus", sampleRate:16000, numberOfChannels:1, bitrate: ...}` configured for VOIP/FEC where the API allows (`opus: {application:"voip", useinbandfec:true}` in `configure` if supported — feature-detect keys). Encoder `output(chunk)` → `A.packFrame({stream_ref: <op.mic ref from index>, seq: seq++&0xFFFF, ts: ts+=samplesPer20ms, flags:0, payload: new Uint8Array(chunk.byteLength)})` and, ONLY if `A.micWantsUplink({micEnabled:true, keyed:store.keyed, youHold:store.youHold})`, `ws.send(frame)`. Set `micEnabled=true`; send `{v:1,type:"mic_open",format:{rate:16000,channels:1},codec:"opus"}` when keying begins.

- [ ] **Step 2b: Gate frames on PTT via the store**

The worklet pushes chunks continuously; the encoder runs continuously; but the WS `send` is guarded by `A.micWantsUplink(...)` each frame (reads `Alpine.store('control')`). On the keyed→un-keyed transition send `{v:1,type:"mic_close"}` once. Use an Alpine `$watch`/effect (or poll the store in the encoder callback) so no frames leak when un-keyed. This reuses the control-panel PTT state machine unchanged — the audio panel never sends `ptt` itself.

- [ ] **Step 3: Sidetone**

`toggleSidetone()`: route the mic source node → a dedicated sidetone `GainNode` (`A.dbToLinear(sidetoneGainDb)`) → master, browser-local only (never sent). Off by default. Sidetone is independent of keying (monitor), but document that most FM ops leave it off.

- [ ] **Step 4: disableMic()** tears down worklet/encoder/track, sends `mic_close`, `micEnabled=false`.

- [ ] **Step 5: Commit**

```bash
git add static/js/mic-worklet.js static/js/audio-panel.js
git commit -m "feat(audio-web): mic capture/encode + PTT-gated uplink + sidetone"
```

---

### Task 8: Operator quickview + mixer matrix UI (pixel + frontend-design)

**Files:**
- Create: `apps/control/templates/control/_audio_panel.html` (partial)
- Modify: `apps/control/templates/control/panel.html` (include partial, load scripts with nonce, pass `data-station-id`)
- Create: `apps/control/templates/control/_audio_selftest.html` + a dev-only view/URL OR a static self-test HTML page under `static/audio-selftest.html` (browser WebCodecs decode of the fixture → OfflineAudioContext FFT → assert 1 kHz peak; logged to the page).

**REQUIRED:** the implementer MUST invoke `Skill("frontend-design")` before writing any markup/CSS (CLAUDE.md rule).

**Interfaces:**
- Consumes: `audioPanel` Alpine component (Task 6/7), `Alpine.store('control')`.
- Produces: the rendered UI.

- [ ] **Step 1: Invoke `Skill("frontend-design")`** and derive the visual direction (fits the existing OE5XRX marine/cyan control-panel aesthetic; reuse existing tokens.css variables and Bootstrap 5 utility classes).

- [ ] **Step 2: `_audio_panel.html` — quickview**

`<div x-data="audioPanel" data-station-id="{{ station.id }}">` containing:
- Connection/gate status pill (`conn`, plus `store.youHold`/`store.keyed` reused for a "TX ready/keyed" badge) — read the store via `$store.control`.
- WebCodecs-unsupported banner (`x-show="!supported"`) with `unsupportedReason`.
- "What am I hearing" — per-stream row: subscribe toggle, live/idle badge (`stream_state`), a gain slider (`<input type="range" lang="en" min="-60" max="12" step="0.5">`, forced dot-decimal), mute button, level meter (`levels[stream_id]`).
- Preset selector (FM / Satellite / Custom) → `applyPreset`.
- "What am I transmitting with" — a `tx_route` `<select>` built from RX-capable modules (`slot`,`module`) → `setTxRoute`; wired through `$store.control.sendCommand` (lock-gated). Show current `txRoute`.
- Mic enable/disable button + sidetone toggle + sidetone gain (dot-decimal input).
- The big PTT is the EXISTING control-panel PTT (reused) — the audio partial sits beside it; do not duplicate the PTT control. Add a one-line hint that PTT/lock live in the control panel.

- [ ] **Step 3: `_audio_panel.html` — expandable mixer matrix**

A collapsible "Mixer" section: sources (rows) × [subscribe, gain dB, mute, level] columns, so an operator sees all sources at once. Bootstrap table/grid; keyboard-focusable controls; ARIA labels for meters/sliders.

- [ ] **Step 4: Wire into `panel.html`**

Include the partial where the control panel renders (beside/below the module widgets). Add `<script src="{% static 'js/audio-logic.js' %}" nonce="{{ request.csp_nonce }}"></script>` (before `audio-panel.js`) and `<script src="{% static 'js/audio-panel.js' %}" nonce=...></script>` after the existing control scripts. `mic-worklet.js` is loaded at runtime via `audioWorklet.addModule('{% static "js/mic-worklet.js" %}')` — pass the URL via a `data-worklet-url` attribute so the JS has no hard-coded static path. Use ONLY `{% comment %}` blocks for any multi-line comment.

- [ ] **Step 5: Self-test page (evidence for WebCodecs decode)**

A standalone page that: fetches `media_frame_slot0rx.bin`, `A.parseFrame`s it, decodes the Opus payload via `AudioDecoder` (8 kHz), renders into an `OfflineAudioContext`, runs an FFT (AnalyserNode or manual DFT), and asserts the dominant bin ≈ 1 kHz — printing `PASS`/`FAIL` + the peak frequency to the page. This is the human-runnable proof that the decode path works (WebCodecs is unavailable in Node/CI). Reference it from the manual checklist.

- [ ] **Step 6: Verify templates render**

Run: `python -m pytest tests/ -k "template or control_panel" -q` (or the existing template-guard test) and `python -m manage.py check`. Confirm no `{# multi-line #}` (the CI template guard must stay green).

- [ ] **Step 7: Commit**

```bash
git add apps/control/templates/control/_audio_panel.html apps/control/templates/control/panel.html static/audio-selftest.html
git commit -m "feat(audio-web): operator quickview + mixer matrix UI + WebCodecs self-test page"
```

---

### Task 9: Verification evidence + manual checklist + handback

**Files:**
- Create: `docs/superpowers/2026-09-05-session-d-web-audio-handback.md`

**Interfaces:**
- Consumes: everything above.
- Produces: the handback report + a documented manual verification checklist (no "looks good" without evidence).

- [ ] **Step 1: Run the full JS + relevant pytest suites, capture output**

Run: `node tests/js/audio-logic.test.mjs` and `python -m pytest tests/test_audio_logic_js.py tests/test_control_logic_js.py -v`. Paste exact output into the handback.

- [ ] **Step 2: Write the manual verification checklist** (human, in a WebCodecs browser):
  1. Open `static/audio-selftest.html` → shows `PASS` + peak ≈ 1000 Hz.
  2. On a station panel: audio panel connects (`conn=open`), `streams` populate, FM preset auto-subscribes `slot0.rx` + `op.mic`.
  3. Gain slider + mute change audible level; level meter tracks.
  4. Satellite preset drops `op.mic`.
  5. Acquire lock + hold PTT (control panel) → mic uplink frames flow ONLY while keyed; releasing PTT stops them; without lock → `error{not_locked}` shown once, not spammed.
  6. `tx_route` select issues a control-WS command (verify in devtools/network) and is rejected when not holding the lock.
  7. Sidetone toggle monitors own mic locally.
  8. Non-WebCodecs browser → unsupported banner, no crash.

- [ ] **Step 3: Write the handback** (branch, PR link placeholder, files changed, decisions incl. target browsers + WASM-fallback deferral, test outputs, the checklist above, open items). Mirror it into the PR body.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/2026-09-05-session-d-web-audio-handback.md
git commit -m "docs(audio-web): Session D web-audio-client handback + manual checklist"
```

---

## Self-Review

- **Spec coverage:** §5.1 endpoint/auth → Task 6. §5.2 hello/subscribe/unsubscribe/mic_open/mic_close + streams/stream_state/error → Task 6/7. §5.3 frame codec byte-identical + fixture → Task 1. §5.4 Opus decode FEC/PLC + seq-loss → Task 3/6. §3 presets + browser-local mix + sidetone → Task 2/6/7. §5.6 tx_route on control-plane, lock-gated → Task 5/8. Operator quickview + matrix → Task 8. WebCodecs target + no WASM now → Global Constraints + Task 6/9. TDD Node suite + fixture cross-check → Task 1–4. Headless decode evidence → Task 8 self-test page (browser, since CI has no WebCodecs) + Task 9 checklist.
- **Placeholder scan:** all code steps contain concrete code or exact instructions; no TBD/TODO.
- **Type consistency:** `packFrame`/`parseFrame` field names (`stream_ref,seq,ts,flags,payload`) consistent across Tasks 1/6/7; `buildStreamIndex` shape reused in Task 6; `micWantsUplink` signature identical in Tasks 2/7; `Alpine.store('control')` field names (`youHold,keyed,canControl,connected,sendCommand,txRoute`) consistent Tasks 5/6/7/8.
