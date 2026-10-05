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
  // 96 input / ratio 3 = exactly 32 output samples: positions 0,3,6,…,93.
  assert.equal(out.length, 32);
  assert.equal(out[0], 0);
  assert.equal(out[1], 3);
  assert.equal(out[2], 6);
  assert.equal(out[31], 93);
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

// --- jitter depth per stream ----------------------------------------------
ok("jitterDepthFor gives op.mic a deeper buffer than RX sources", () => {
  const opMic = { stream_id: "op.mic", module: "operator", direction: "rx" };
  const rx = { stream_id: "slot1.rx", module: "fm", direction: "rx" };
  const micDepth = A.jitterDepthFor(opMic);
  const rxDepth = A.jitterDepthFor(rx);
  assert.equal(rxDepth, 3);
  // Floor, not exact: guards against silently REDUCING op.mic buffering below
  // the depth chosen to absorb encoder burstiness (which would reintroduce
  // stutter), while still allowing a future increase for more smoothing.
  assert.ok(micDepth >= 5, `op.mic depth ${micDepth} >= 5`);
  assert.ok(micDepth > rxDepth, `mic ${micDepth} > rx ${rxDepth}`);
});

// --- link-quality stats ---------------------------------------------------
ok("linkStats starts at zero", () => {
  const s = A.makeLinkStats();
  const snap = A.linkSnapshot(s, 0);
  assert.deepEqual(
    [snap.recv, snap.lost, snap.reorder, snap.conceal, snap.underruns, snap.jitterMs],
    [0, 0, 0, 0, 0, 0]
  );
});

ok("linkStats jitter is ~0 for a steady 20 ms cadence, rises when irregular", () => {
  const steady = A.makeLinkStats();
  for (let t = 0; t <= 200; t += 20) A.linkRecord(steady, "recv", { tMs: t });
  assert.ok(A.linkSnapshot(steady, 200).jitterMs < 0.001, "steady jitter ~0");

  const bursty = A.makeLinkStats();
  const times = [0, 5, 40, 45, 90, 95]; // clumped arrivals
  for (const t of times) A.linkRecord(bursty, "recv", { tMs: t });
  assert.ok(A.linkSnapshot(bursty, 95).jitterMs > 1, "bursty jitter rises");
});

ok("linkStats totals accumulate; reorder is total-only (not windowed)", () => {
  const s = A.makeLinkStats();
  A.linkRecord(s, "lost", { n: 2, tMs: 1000 });
  A.linkRecord(s, "conceal", { n: 1, tMs: 1000 });
  A.linkRecord(s, "underrun", { tMs: 1000 });
  A.linkRecord(s, "reorder", { n: 3, tMs: 1000 });
  const snap = A.linkSnapshot(s, 1000);
  assert.equal(snap.lost, 2);
  assert.equal(snap.conceal, 1);
  assert.equal(snap.underruns, 1);
  assert.equal(snap.reorder, 3);
  assert.deepEqual(snap.win, { lost: 2, conceal: 1, underruns: 1 });
});

ok("linkStats rolling window drops events older than 5 s", () => {
  const s = A.makeLinkStats();
  A.linkRecord(s, "lost", { n: 1, tMs: 0 });
  A.linkRecord(s, "underrun", { tMs: 500 });
  // At t=6000 both are >5 s old → window empty, totals unchanged.
  const snap = A.linkSnapshot(s, 6000);
  assert.deepEqual(snap.win, { lost: 0, conceal: 0, underruns: 0 });
  assert.equal(snap.lost, 1);
  assert.equal(snap.underruns, 1);
});

// --- seq math (u16 wrap) ---------------------------------------------------
ok("seqDelta wrap-aware", () => {
  assert.equal(A.seqDelta(10, 11), 1);
  assert.equal(A.seqDelta(10, 10), 0);
  assert.equal(A.seqDelta(11, 10), -1);
  assert.equal(A.seqDelta(65535, 0), 1);
  assert.equal(A.seqDelta(0, 65535), -1);
});
// --- T0/T1 dBFS taps + inject (Task 10) -----------------------------------
ok("rmsToDbfs full-scale ~ -3dB at 0.707", function(){ assert(Math.abs(A.rmsToDbfs(0.7071) - (-3.01)) < 0.1); });
ok("rmsToDbfs silence is null", function(){ assert.strictEqual(A.rmsToDbfs(0), null); });
ok("dbfsToAmplitude -20 ~ 0.1", function(){ assert(Math.abs(A.dbfsToAmplitude(-20) - 0.1) < 0.001); });
ok("buildTapReport shape", function(){ var r=A.buildTapReport("T1",{rms:0.1,peak:0.1,rate:48000,windowMs:300,constraints:{}}); assert.strictEqual(r.point,"T1"); assert.strictEqual(r.format.rate,48000); assert.strictEqual(r.format.channels,1); assert.strictEqual(r.silent,false); });
ok("buildTapReport silence flag", function(){ var r=A.buildTapReport("T0",{rms:0,peak:0,rate:48000,windowMs:300}); assert.strictEqual(r.silent,true); assert.strictEqual(r.rms_dbfs,null); });
ok("buildTapReport undefined rms is silent", function(){ var r=A.buildTapReport("T1",{rate:48000,windowMs:300}); assert.strictEqual(r.silent,true); assert.strictEqual(r.rms_dbfs,null); });
ok("captureConstraints picks three keys", function(){ var c=A.captureConstraintsFromSettings({autoGainControl:true,noiseSuppression:false,echoCancellation:true,sampleRate:48000}); assert.strictEqual(c.autoGainControl,true); assert.strictEqual(c.noiseSuppression,false); assert.strictEqual(c.echoCancellation,true); assert.strictEqual(c.sampleRate,undefined); });
ok("buildTapReport window_ms passthrough (simulates worklet diag_tap conversion)", function(){
  // The port.onmessage branch in audio-panel.js calls
  //   A.buildTapReport(d.point, {rms:d.rms, peak:d.peak, rate:<contextRate>, windowMs:d.window_ms})
  // Verify that window_ms is preserved exactly and dBFS values are correct.
  var rms = 0.5, peak = 0.9, windowMs = 250, rate = 48000;
  var r = A.buildTapReport("T1", { rms: rms, peak: peak, rate: rate, windowMs: windowMs });
  assert.strictEqual(r.point, "T1");
  assert.strictEqual(r.window_ms, windowMs);
  assert.strictEqual(r.format.rate, rate);
  assert.ok(Math.abs(r.rms_dbfs - 20 * Math.log10(rms)) < 1e-6, "rms_dbfs matches 20log10(rms)");
  assert.ok(Math.abs(r.peak_dbfs - 20 * Math.log10(peak)) < 1e-6, "peak_dbfs matches 20log10(peak)");
  assert.strictEqual(r.silent, false);
  // Silence case: worklet posts rms=0 (no audio in window).
  var silent = A.buildTapReport("T1", { rms: 0, peak: 0, rate: rate, windowMs: windowMs });
  assert.strictEqual(silent.rms_dbfs, null);
  assert.strictEqual(silent.silent, true);
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

// --- Browser capture (DSP off + fixed gain) and TX hub meter view ---------
ok("micCaptureConstraints is exactly mono with all native DSP off", () => {
  assert.deepEqual(A.micCaptureConstraints(), {
    channelCount: 1,
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
  });
});
ok("capture gain is unity until calibrated on-station", () => {
  assert.strictEqual(A.MIC_CAPTURE_GAIN_DB, 0);
  assert.strictEqual(A.captureGainLinear(), 1);
});
ok("txMeterStale: stale only after maxAge without a frame", () => {
  assert.strictEqual(A.txMeterStale(1000, 1500), false);
  assert.strictEqual(A.txMeterStale(1000, 2000), false); // exactly 1 s: not yet
  assert.strictEqual(A.txMeterStale(1000, 2001), true);
  assert.strictEqual(A.txMeterStale(1000, 1300, 200), true);
  assert.strictEqual(A.txMeterStale(null, 5000), false); // never received: nothing to expire
  assert.strictEqual(A.txMeterStale("x", 5000), false);
});
ok("captureGainLinear matches MIC_CAPTURE_GAIN_DB", () => {
  assert.ok(Math.abs(A.captureGainLinear() - Math.pow(10, A.MIC_CAPTURE_GAIN_DB / 20)) < 1e-9);
});
ok("txMeterView: at the ceiling is a full bar and carries limiting", () => {
  const v = A.txMeterView({ type: "tx_meter", active: true, peak_dbfs: -12, ceiling_dbfs: -12,
                            gain_reduction_db: 4, limiting: true, dsp: "full" });
  assert.strictEqual(v.hubFrac, 1);
  assert.strictEqual(v.limiting, true);
  assert.strictEqual(v.grDb, 4);
  assert.strictEqual(v.degraded, false);
  assert.strictEqual(v.failed, false);
});
ok("txMeterView: bar spans the 30 dB below the ceiling", () => {
  const q = A.txMeterView({ active: true, peak_dbfs: -27, ceiling_dbfs: -12, dsp: "degraded" });
  assert.ok(Math.abs(q.hubFrac - 0.5) < 1e-9);
  assert.strictEqual(A.txMeterView({ active: true, peak_dbfs: -80, ceiling_dbfs: -12 }).hubFrac, 0);
  assert.strictEqual(A.txMeterView({ active: true, peak_dbfs: 0, ceiling_dbfs: -12 }).hubFrac, 1);
});
ok("txMeterView: degraded for degraded/off, failed only for failed", () => {
  const f = (dsp) => A.txMeterView({ active: true, peak_dbfs: -20, ceiling_dbfs: -12, dsp });
  assert.deepEqual([f("degraded").degraded, f("degraded").failed], [true, false]);
  assert.deepEqual([f("off").degraded, f("off").failed], [true, false]);
  assert.deepEqual([f("failed").degraded, f("failed").failed], [false, true]);
  assert.deepEqual([f("full").degraded, f("full").failed], [false, false]);
});
ok("txMeterView: first frame (null peak) is active with an empty bar", () => {
  const s = A.txMeterView({ active: true, peak_dbfs: null, ceiling_dbfs: -12, dsp: "full" });
  assert.strictEqual(s.active, true);
  assert.strictEqual(s.hubFrac, 0);
  assert.strictEqual(s.ceilingDbfs, -12);
});
ok("txMeterView: missing/garbage ceiling falls back to -12", () => {
  assert.strictEqual(A.txMeterView({ active: true, peak_dbfs: -12, dsp: "full" }).hubFrac, 1);
  for (const c of ["x", NaN, Infinity, null]) {
    const v = A.txMeterView({ active: true, peak_dbfs: -12, ceiling_dbfs: c, dsp: "full" });
    assert.strictEqual(v.hubFrac, 1);
    assert.strictEqual(v.ceilingDbfs, -12);
  }
});
ok("txMeterView: inactive/garbage input is a safe empty view", () => {
  for (const junk of [null, undefined, "x", {}, { active: false }, { active: false, dsp: "failed" }]) {
    const j = A.txMeterView(junk);
    assert.strictEqual(j.active, false);
    assert.strictEqual(j.hubFrac, 0);
    assert.strictEqual(j.limiting, false);
    assert.strictEqual(j.degraded, false);
    assert.strictEqual(j.failed, false);
  }
  const g = A.txMeterView({ active: true, peak_dbfs: "x", gain_reduction_db: "y" });
  assert.ok(g.hubFrac >= 0 && g.hubFrac <= 1 && !Number.isNaN(g.hubFrac));
  assert.strictEqual(g.grDb, 0);
});

// --- Review round 1: watchdog decays LEVELS only; DSP status stays sticky ----------
ok("txMeterDecay: zeroes levels but keeps failed/degraded/ceiling/active", () => {
  const failed = A.txMeterView({ active: true, peak_dbfs: -14, ceiling_dbfs: -18,
                                 gain_reduction_db: 3, limiting: true, dsp: "failed" });
  const d = A.txMeterDecay(failed);
  assert.deepEqual(d, { active: true, hubFrac: 0, peakDbfs: null, grDb: 0, limiting: false,
                        degraded: false, failed: true, ceilingDbfs: -18 });
  const deg = A.txMeterDecay(A.txMeterView({ active: true, peak_dbfs: -20, ceiling_dbfs: -12,
                                             dsp: "off" }));
  assert.strictEqual(deg.degraded, true);
  assert.strictEqual(deg.active, true);
  assert.strictEqual(deg.hubFrac, 0);
});
ok("txMeterDecay: does not mutate its input", () => {
  const v = A.txMeterView({ active: true, peak_dbfs: -12, ceiling_dbfs: -12, limiting: true });
  A.txMeterDecay(v);
  assert.strictEqual(v.hubFrac, 1);
  assert.strictEqual(v.limiting, true);
});
ok("txMeterDecay: already-decayed view is returned as-is (no reactive churn)", () => {
  const d = A.txMeterDecay(A.txMeterView({ active: true, peak_dbfs: -12, dsp: "failed" }));
  assert.strictEqual(A.txMeterDecay(d), d);
});
ok("txMeterDecay: inactive/garbage input is the safe empty view", () => {
  for (const junk of [null, undefined, "x", {}, A.txMeterView(null)]) {
    assert.deepEqual(A.txMeterDecay(junk), A.txMeterView(null));
  }
});

console.log("\n" + passed + " assertions passed");
