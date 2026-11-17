// Behavioural test: RX playback scheduling in static/js/audio-panel.js — lead
// cap / drift compensation, suspended-context handling (iOS/autoplay), the
// lead/drop/catch-up link stats, and the TX-route select handler.
// Loads the real component against minimal window/document/Alpine/WebAudio
// stubs (pattern: tests/js/audio-panel-txmeter.test.mjs).
// Run: node tests/js/audio-panel-rxsched.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));

const listeners = {};
globalThis.window = {};
globalThis.document = {
  addEventListener: (n, fn) => {
    listeners[n] = fn;
  },
};
const A = require(path.resolve(here, "../../static/js/audio-logic.js"));
window.OE5XRXAudioLogic = A;
require(path.resolve(here, "../../static/js/audio-panel.js"));

let factory = null;
window.Alpine = {
  data: (name, fn) => {
    if (name === "audioPanel") factory = fn;
  },
};
listeners["alpine:init"]();
assert.ok(factory, "audioPanel registered");

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

// --- WebAudio stubs ----------------------------------------------------------
function makeAudioCtx(state) {
  const ctx = {
    state: state,
    currentTime: 10,
    resumes: 0,
    started: [],
    _listeners: {},
    resume() {
      this.resumes += 1;
      return Promise.resolve();
    },
    addEventListener(type, fn) {
      this._listeners[type] = fn;
    },
    removeEventListener(type) {
      delete this._listeners[type];
    },
    createBuffer(channels, frames, rate) {
      const data = [];
      for (let c = 0; c < channels; c++) data.push(new Float32Array(frames));
      return { duration: frames / rate, getChannelData: (c) => data[c] };
    },
    createBufferSource() {
      const src = {
        buffer: null,
        playbackRate: { value: 1 },
        stopped: false,
        onended: null,
        connect() {},
        disconnect() {},
        start(at) {
          src.at = at;
          ctx.started.push(src);
        },
        stop() {
          src.stopped = true;
        },
      };
      return src;
    },
  };
  return ctx;
}

// 20 ms @ 8 kHz; amp = constant sample value (RMS == |amp|).
function audioData(amp) {
  return {
    numberOfChannels: 1,
    numberOfFrames: 160,
    sampleRate: 8000,
    copyTo(dst) {
      dst.fill(amp);
    },
    close() {},
  };
}

function panelWithStream(state, bufferAhead) {
  const c = factory();
  c._audioCtx = makeAudioCtx(state);
  c._streamCtx["slot3.rx"] = {
    gainNode: {},
    playhead: c._audioCtx.currentTime + bufferAhead,
    bufferAhead: bufferAhead,
    stats: A.makeLinkStats(),
    jitter: A.createJitter({ depth: 3 }),
  };
  return c;
}

const LOUD = 0.3;
const QUIET = 0.0005;

ok("in-target frame is scheduled at the playhead at unity rate", () => {
  const c = panelWithStream("running", 0.06);
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  const ac = c._audioCtx;
  assert.equal(ac.started.length, 1);
  assert.ok(Math.abs(ac.started[0].at - 10.06) < 1e-9);
  assert.equal(ac.started[0].playbackRate.value, 1);
  assert.ok(Math.abs(c._streamCtx["slot3.rx"].playhead - 10.08) < 1e-9);
});

ok("underrun re-primes a full bufferAhead and is counted", () => {
  const c = panelWithStream("running", 0.06);
  c._streamCtx["slot3.rx"].playhead = 9;
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.ok(Math.abs(c._audioCtx.started[0].at - 10.06) < 1e-9);
  assert.equal(c._streamCtx["slot3.rx"].stats.underruns, 1);
});

ok("suspended context: frame dropped, resume() requested, playhead forgotten", () => {
  const c = panelWithStream("suspended", 0.06);
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.equal(c._audioCtx.started.length, 0, "nothing scheduled into a suspended context");
  assert.equal(c._audioCtx.resumes, 1);
  assert.equal(c._streamCtx["slot3.rx"].playhead, null);
  // Once running again the next frame primes fresh — not an underrun.
  c._audioCtx.state = "running";
  c._audioCtx.currentTime = 42;
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.ok(Math.abs(c._audioCtx.started[0].at - 42.06) < 1e-9);
  assert.equal(c._streamCtx["slot3.rx"].stats.underruns, 0);
});

ok("suspend after queuing: queued sources are stopped (no overlap after resume)", () => {
  const c = panelWithStream("running", 0.06);
  for (let i = 0; i < 3; i++) c._onDecodedAudio("slot3.rx", audioData(LOUD));
  const queued = c._audioCtx.started.slice();
  c._audioCtx.state = "suspended";
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  for (const s of queued) assert.equal(s.stopped, true);
  assert.equal(c._streamCtx["slot3.rx"].sources.length, 0);
});

ok("iOS 'interrupted' context is treated like suspended", () => {
  const c = panelWithStream("interrupted", 0.06);
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.equal(c._audioCtx.started.length, 0);
  assert.equal(c._audioCtx.resumes, 1);
});

ok("level meter still updates for a dropped (suspended) frame", () => {
  const c = panelWithStream("suspended", 0.06);
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.ok(c.levels["slot3.rx"] > 0);
});

ok("soft excess: quiet frame dropped (counted), loud frame sped up", () => {
  const c = panelWithStream("running", 0.06);
  const sc = c._streamCtx["slot3.rx"];
  sc.playhead = 10 + 0.06 + A.RX_LEAD_SOFT_S + 0.01;
  const ph = sc.playhead;
  c._onDecodedAudio("slot3.rx", audioData(QUIET));
  assert.equal(c._audioCtx.started.length, 0);
  assert.equal(sc.playhead, ph);
  assert.equal(sc.stats.drops, 1);
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.equal(c._audioCtx.started.length, 1);
  assert.equal(c._audioCtx.started[0].playbackRate.value, A.RX_CATCHUP_RATE);
  assert.equal(sc.stats.speedups, 1);
});

ok("hard excess: future sources are stopped, the playing one finishes, stream re-anchors", () => {
  const c = panelWithStream("running", 0.06);
  // Queue a few frames normally, then fake a large accumulated lead.
  for (let i = 0; i < 3; i++) c._onDecodedAudio("slot3.rx", audioData(LOUD));
  const queued = c._audioCtx.started.slice();
  // Clock moves into the first frame: it is now playing.
  c._audioCtx.currentTime = queued[0].at + 0.005;
  const sc = c._streamCtx["slot3.rx"];
  sc.playhead = c._audioCtx.currentTime + 0.06 + A.RX_LEAD_HARD_S + 0.5;
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.equal(queued[0].stopped, false, "currently playing source is not cut (click)");
  assert.equal(queued[1].stopped, true);
  assert.equal(queued[2].stopped, true);
  const last = c._audioCtx.started[c._audioCtx.started.length - 1];
  assert.equal(last.stopped, false);
  assert.ok(Math.abs(last.at - (c._audioCtx.currentTime + 0.06)) < 1e-9);
  assert.equal(sc.stats.catchups, 1);
  assert.deepEqual(sc.sources, [queued[0], last], "playing + fresh source still tracked");
});

ok("finished sources are untracked (no unbounded growth)", () => {
  const c = panelWithStream("running", 0.06);
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  const sc = c._streamCtx["slot3.rx"];
  assert.equal(sc.sources.length, 1);
  c._audioCtx.started[0].onended();
  assert.equal(sc.sources.length, 0);
});

ok("steady 0.6 % drift keeps the lead bounded end-to-end", () => {
  const c = panelWithStream("running", 0.06);
  const ac = c._audioCtx;
  const sc = c._streamCtx["slot3.rx"];
  let maxLead = 0;
  for (let i = 0; i < 15000; i++) {
    // 5 min of audio arriving 0.6 % fast; alternate talk and pauses.
    ac.currentTime += 0.02 / 1.006;
    c._onDecodedAudio("slot3.rx", audioData(i % 100 < 60 ? LOUD : QUIET));
    // Sources end once played; keep the tracking list realistic.
    while (sc.sources.length && sc.sources[0].at + 0.02 < ac.currentTime) sc.sources[0].onended();
    maxLead = Math.max(maxLead, sc.playhead - ac.currentTime);
  }
  assert.ok(maxLead < 0.06 + A.RX_LEAD_SOFT_S + 0.03, "maxLead " + maxLead);
  assert.equal(sc.stats.catchups, 0);
  assert.ok(sc.sources.length < 20);
});

ok("PLC on a forgotten playhead keeps it unprimed (no NaN)", () => {
  const c = panelWithStream("suspended", 0.06);
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  c._concealFrame("slot3.rx", 0);
  assert.equal(c._streamCtx["slot3.rx"].playhead, null);
  const c2 = panelWithStream("running", 0.06);
  c2._concealFrame("slot3.rx", 0);
  assert.ok(Math.abs(c2._streamCtx["slot3.rx"].playhead - 10.08) < 1e-9);
  assert.equal(c2._streamCtx["slot3.rx"].stats.conceal, 1);
});

ok("link snapshot reports current lead (ms) and drop/catch-up counters", () => {
  const c = panelWithStream("running", 0.06);
  c._streamCtx["slot3.rx"].playhead = 10.25;
  c._refreshLinkStats();
  const st = c.linkStats["slot3.rx"];
  assert.equal(st.leadMs, 250);
  assert.equal(st.drops, 0);
  assert.equal(st.catchups, 0);
  assert.equal(st.win.drops, 0);
  c._streamCtx["slot3.rx"].playhead = null;
  c._refreshLinkStats();
  assert.equal(c.linkStats["slot3.rx"].leadMs, null);
  c._streamCtx["slot3.rx"].playhead = 9; // behind = no lead, never negative
  c._refreshLinkStats();
  assert.equal(c.linkStats["slot3.rx"].leadMs, 0);
});

ok("autoplay unlock: a user gesture resumes a non-running context", () => {
  const added = {};
  window.addEventListener = (type, fn) => {
    added[type] = fn;
  };
  window.removeEventListener = () => {};
  const c = factory();
  c._audioCtx = makeAudioCtx("suspended");
  c._installAutoplayUnlock();
  for (const t of ["pointerdown", "touchend", "keydown"]) assert.equal(typeof added[t], "function", t);
  added.pointerdown();
  assert.equal(c._audioCtx.resumes, 1);
  c._audioCtx.state = "running";
  added.keydown();
  assert.equal(c._audioCtx.resumes, 1, "running context is left alone");
  // iOS: an interruption ending fires statechange → try to resume.
  c._audioCtx.state = "interrupted";
  c._audioCtx._listeners.statechange();
  assert.equal(c._audioCtx.resumes, 2);
});

ok("autoplay unlock: listeners are removed on teardown", () => {
  const added = [], removed = [];
  window.addEventListener = (type, fn) => added.push([type, fn]);
  window.removeEventListener = (type, fn) => removed.push([type, fn]);
  window.clearInterval = () => {};
  const c = factory();
  const ac = makeAudioCtx("suspended");
  ac.close = () => {};
  const acRemoved = [];
  ac.removeEventListener = (type) => acRemoved.push(type);
  c._audioCtx = ac;
  c._installAutoplayUnlock();
  c._teardown();
  for (const [type, fn] of added) {
    assert.ok(removed.some(([t, f]) => t === type && f === fn), "removed " + type);
  }
  assert.deepEqual(added.map(([t]) => t).sort(), ["keydown", "pointerdown", "touchend"]);
  assert.deepEqual(acRemoved, ["statechange"]);
});

ok("per-frame resume() is not re-issued while one is in flight; gestures always retry", () => {
  const added = {};
  window.addEventListener = (type, fn) => {
    added[type] = fn;
  };
  const c = panelWithStream("suspended", 0.06);
  // A context that is not allowed to start keeps its resume() promise pending.
  c._audioCtx.resume = function () {
    this.resumes += 1;
    return new Promise(() => {});
  };
  c._installAutoplayUnlock();
  for (let i = 0; i < 5; i++) c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.equal(c._audioCtx.resumes, 1, "one in-flight resume, not one per frame");
  added.pointerdown();
  assert.equal(c._audioCtx.resumes, 2, "a gesture retries regardless");
});

ok("panel carries the catch-up flag between frames (hysteresis wiring)", () => {
  const mid = 0.06 + (A.RX_LEAD_SOFT_EXIT_S + A.RX_LEAD_SOFT_S) / 2;
  const c = panelWithStream("running", 0.06);
  const sc = c._streamCtx["slot3.rx"];
  // Not catching up: a lead between exit and enter thresholds plays at 1.0.
  sc.playhead = 10 + mid;
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.equal(c._audioCtx.started[0].playbackRate.value, 1);
  // Enter catch-up above SOFT, then the same mid lead keeps playing fast.
  sc.playhead = 10 + 0.06 + A.RX_LEAD_SOFT_S + 0.01;
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.equal(sc.catchingUp, true);
  sc.playhead = 10 + mid;
  c._onDecodedAudio("slot3.rx", audioData(LOUD));
  assert.equal(c._audioCtx.started[2].playbackRate.value, A.RX_CATCHUP_RATE);
});

ok("TX route select: JSON value → setTxRoute(slot, module); empty/garbage clears", () => {
  const c = factory();
  const calls = [];
  c.setTxRoute = (slot, module) => calls.push([slot, module]);
  c.onTxRouteChange(JSON.stringify({ slot: 2, module: "fm" }));
  c.onTxRouteChange("");
  c.onTxRouteChange("{not json");
  assert.deepEqual(calls, [[2, "fm"], [null, null], [null, null]]);
});

console.log("\n" + passed + " assertions passed");
