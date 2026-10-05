// Behavioural test: TX-meter + capture-gain wiring in static/js/audio-panel.js.
// Loads the real component against minimal window/document/Alpine/WebAudio stubs
// (pattern: tests/js/control-panel-readonly.test.mjs).
// Run: node tests/js/audio-panel-txmeter.test.mjs  (exit 0 = pass).

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
async function ok(name, fn) {
  await fn();
  passed += 1;
  console.log("ok - " + name);
}

const FAILED = { type: "tx_meter", active: true, peak_dbfs: -14, ceiling_dbfs: -18,
                 gain_reduction_db: 2, limiting: true, dsp: "failed" };
const LIVE = { type: "tx_meter", active: true, peak_dbfs: -12, ceiling_dbfs: -12,
               gain_reduction_db: 4, limiting: true, dsp: "full" };

await ok("tx_meter frame dispatch updates txMeter and its timestamp", () => {
  const c = factory();
  c._routeJSON(LIVE);
  assert.equal(c.txMeter.active, true);
  assert.equal(c.txMeter.hubFrac, 1);
  assert.equal(c.txMeter.limiting, true);
  assert.equal(typeof c._txMeterAt, "number");
});

await ok("watchdog: fresh meter is left alone", () => {
  const c = factory();
  c._routeJSON(LIVE);
  const before = c.txMeter;
  c._txMeterTick(c._txMeterAt + 500);
  assert.equal(c.txMeter, before);
});

await ok("watchdog: stale meter decays levels but keeps the failed status sticky", () => {
  const c = factory();
  c._routeJSON(FAILED);
  c._txMeterTick(c._txMeterAt + 1500);
  assert.equal(c.txMeter.active, true);
  assert.equal(c.txMeter.failed, true);
  assert.equal(c.txMeter.ceilingDbfs, -18);
  assert.equal(c.txMeter.hubFrac, 0);
  assert.equal(c.txMeter.peakDbfs, null);
  assert.equal(c.txMeter.limiting, false);
  // Later ticks keep it (and don't churn the reactive object).
  const decayed = c.txMeter;
  c._txMeterTick(c._txMeterAt + 60000);
  assert.equal(c.txMeter, decayed);
});

await ok("watchdog: degraded ('DSP off') pill survives a silent DTX gap", () => {
  const c = factory();
  c._routeJSON({ ...LIVE, dsp: "off" });
  c._txMeterTick(c._txMeterAt + 5000);
  assert.equal(c.txMeter.degraded, true);
  assert.equal(c.txMeter.hubFrac, 0);
});

await ok("explicit active:false frame clears the sticky status", () => {
  const c = factory();
  c._routeJSON(FAILED);
  c._txMeterTick(c._txMeterAt + 1500);
  c._routeJSON({ type: "tx_meter", active: false });
  assert.equal(c.txMeter.active, false);
  assert.equal(c.txMeter.failed, false);
});

await ok("agent-disconnected stream_state resets the meter", () => {
  const c = factory();
  c._routeJSON(FAILED);
  c._routeJSON({ type: "stream_state", stream_id: "s0", state: "idle",
                 detail: "agent disconnected" });
  assert.equal(c.txMeter.active, false);
  assert.equal(c.txMeter.failed, false);
});

await ok("WebSocket close resets the meter", () => {
  const sockets = [];
  globalThis.location = { protocol: "http:", host: "example" };
  globalThis.WebSocket = class {
    constructor(url) {
      this.url = url;
      this.handlers = {};
      sockets.push(this);
    }
    addEventListener(t, fn) {
      this.handlers[t] = fn;
    }
  };
  const c = factory();
  c._stationId = "7";
  c._connect();
  c._closed = true; // no reconnect timer in the test
  c._routeJSON(FAILED);
  sockets[0].handlers.close({ code: 1006 });
  assert.equal(c.txMeter.active, false);
  assert.equal(c.txMeter.failed, false);
});

await ok("enableMic inserts a unity capture GainNode between source and worklet", async () => {
  const edges = [];
  const node = (name, extra = {}) => ({
    name,
    connect(dst) {
      edges.push([name, dst.name]);
    },
    disconnect() {},
    ...extra,
  });
  const gains = [];
  class FakeCtx {
    constructor() {
      this.sampleRate = 48000;
      this.destination = node("destination");
      this.audioWorklet = { addModule: () => Promise.resolve() };
    }
    createMediaStreamSource() {
      return node("source");
    }
    createGain() {
      const g = node("gain" + gains.length, { gain: { value: NaN } });
      gains.push(g);
      return g;
    }
    close() {}
  }
  window.AudioContext = FakeCtx;
  window.AudioWorkletNode = function () {
    return node("worklet", { port: {} });
  };
  window.AudioEncoder = function () {
    return { configure() {}, close() {}, state: "configured" };
  };
  globalThis.navigator = {
    mediaDevices: { getUserMedia: () => Promise.resolve({ getTracks: () => [] }) },
  };

  const c = factory();
  c._workletUrl = "/static/js/mic-worklet.js";
  c._startMicMeter = () => {};
  c.enableMic();
  for (let i = 0; i < 20 && !c.micEnabled; i++) await new Promise((r) => setTimeout(r, 0));
  assert.equal(c.micEnabled, true, "mic enabled: " + c.micError);
  const cg = c._micCaptureGain;
  assert.ok(cg, "capture gain node created");
  assert.equal(cg.gain.value, 1.0);
  assert.equal(cg.gain.value, A.captureGainLinear());
  assert.ok(edges.some(([s, d]) => s === "source" && d === cg.name), "source → capture gain");
  assert.ok(edges.some(([s, d]) => s === cg.name && d === "worklet"), "capture gain → worklet");
  assert.ok(!edges.some(([s, d]) => s === "source" && d === "worklet"), "no gain bypass");
  // Mic close resets a sticky meter.
  c._routeJSON(FAILED);
  c._disableMicInternal(false);
  assert.equal(c.txMeter.active, false);
});

console.log("\n" + passed + " assertions passed");
