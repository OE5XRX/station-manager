// Pure-logic unit tests for static/js/audio-logic.js.
// Run: node tests/js/audio-logic.test.mjs  (exit 0 = pass).
// Invoked from pytest via tests/test_audio_logic_js.py.

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
ok("isRxSource excludes op.mic even with direction rx", () => {
  assert.equal(A.isRxSource({ module:"operator", direction:"rx", stream_id:"op.mic" }), false);
  assert.equal(A.isRxSource({ module:"fm", direction:"rx", stream_id:"slot0.rx" }), true);
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

ok("micLevelFromRms maps RMS to a clamped 0..1 meter level", () => {
  // Silence / non-signal → 0.
  assert.equal(A.micLevelFromRms(0), 0);
  // Fixed ×4 scale below the clamp.
  assert.equal(A.micLevelFromRms(0.1), 0.4);
  assert.equal(A.micLevelFromRms(0.25), 1); // exactly at the ceiling
  // Loud input clamps to 1 (never overshoots the bar).
  assert.equal(A.micLevelFromRms(0.5), 1);
  assert.equal(A.micLevelFromRms(1), 1);
  // Monotonic in the linear region.
  assert.ok(A.micLevelFromRms(0.05) < A.micLevelFromRms(0.15));
});

ok("micLevelFromRms rejects garbage / negative / non-finite input", () => {
  assert.equal(A.micLevelFromRms(-0.3), 0);
  assert.equal(A.micLevelFromRms(NaN), 0);
  assert.equal(A.micLevelFromRms(Infinity), 0);
  assert.equal(A.micLevelFromRms(undefined), 0);
  assert.equal(A.micLevelFromRms("0.2"), 0); // strings are not accepted
});

// --- streaming resampler --------------------------------------------------
ok("resample 48k→16k decimates a ramp by ~3 with linear interpolation", () => {
  const st = A.makeResampler(48000, 16000); // ratio 3
  const input = new Float32Array(96); // 2 ms @ 48k
  for (let i = 0; i < input.length; i++) input[i] = i; // ramp 0..95
  const out = A.resample(st, input);
  // ~96/3 = 32 output samples, first is input[0], then step of 3.
  assert.ok(Math.abs(out.length - 32) <= 1, `len ${out.length}`);
  assert.equal(out[0], 0);
  assert.equal(out[1], 3);
  assert.equal(out[2], 6);
});

ok("resample holds a constant signal exactly", () => {
  const st = A.makeResampler(48000, 16000);
  const input = new Float32Array(48).fill(0.5);
  const out = A.resample(st, input);
  for (let i = 0; i < out.length; i++) {
    assert.ok(Math.abs(out[i] - 0.5) < 1e-6, `sample ${i}=${out[i]}`);
  }
});

ok("resample is phase-continuous across chunk boundaries", () => {
  // Resampling [a, b] as two streamed chunks must equal resampling a+b whole.
  const whole = new Float32Array(240);
  for (let i = 0; i < whole.length; i++) whole[i] = Math.sin(i / 5);

  const one = A.resample(A.makeResampler(48000, 16000), whole);

  const st = A.makeResampler(48000, 16000);
  const partA = A.resample(st, whole.slice(0, 130));
  const partB = A.resample(st, whole.slice(130));
  const streamed = new Float32Array(partA.length + partB.length);
  streamed.set(partA, 0);
  streamed.set(partB, partA.length);

  assert.equal(streamed.length, one.length);
  for (let i = 0; i < one.length; i++) {
    assert.ok(Math.abs(one[i] - streamed[i]) < 1e-6, `mismatch at ${i}`);
  }
});

ok("resample ratio 1 (16k→16k) passes samples through in order", () => {
  const st = A.makeResampler(16000, 16000);
  const a = A.resample(st, new Float32Array([1, 2, 3, 4]));
  const b = A.resample(st, new Float32Array([5, 6]));
  const all = [...a, ...b];
  // Continuous linear interp at ratio 1 reproduces the input sequence
  // (it lags at most one sample at the tail waiting for the next chunk).
  assert.deepEqual(all.slice(0, 5), [1, 2, 3, 4, 5]);
});

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

console.log("\n" + passed + " assertions passed");
