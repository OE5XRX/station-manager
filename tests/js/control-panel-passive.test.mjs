// Behavioural test: passive-service badge (control-panel.js) and the muted RX mixer
// (audio-panel.js), driven by passive_state frames.
// Run: node tests/js/control-panel-passive.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));

const listeners = {};
const events = [];
globalThis.window = {
  dispatchEvent: (ev) => events.push(ev),
};
globalThis.CustomEvent = class {
  constructor(type, init) {
    this.type = type;
    this.detail = init && init.detail;
  }
};
globalThis.document = {
  addEventListener: (n, fn) => {
    (listeners[n] = listeners[n] || []).push(fn);
  },
};
window.OE5XRXControlLogic = require(path.resolve(here, "../../static/js/control-logic.js"));
const A = require(path.resolve(here, "../../static/js/audio-logic.js"));
window.OE5XRXAudioLogic = A;
require(path.resolve(here, "../../static/js/control-panel.js"));
require(path.resolve(here, "../../static/js/audio-panel.js"));

const factories = {};
window.Alpine = {
  store: () => ({}),
  data: (name, fn) => {
    factories[name] = fn;
  },
};
listeners["alpine:init"].forEach((fn) => fn());
assert.ok(factories.controlPanel && factories.audioPanel, "components registered");

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

function makeAudio() {
  const a = factories.audioPanel();
  a._index = A.buildStreamIndex({
    streams: [
      { stream_id: "s1", stream_ref: "r1", direction: "rx", slot: 1, module: "fm" },
      { stream_id: "s2", stream_ref: "r2", direction: "rx", slot: 2, module: "sat" },
      { stream_id: "op.mic", stream_ref: "mic", direction: "tx", module: "operator" },
    ],
  });
  for (const id of ["s1", "s2", "op.mic"]) a.mixer[id] = A.defaultMixerEntry();
  return a;
}
const frame = (state, reason = null) => ({ type: "passive_state", service: "aprs", state, slot: "1", reason });

ok("panel shows the badge and re-emits the frame on a passive_state frame", () => {
  events.length = 0;
  const c = factories.controlPanel();
  c._route(frame("running"));
  assert.equal(c.passive.label, "Passiv: APRS aktiv");
  assert.equal(c.passive.level, "ok");
  assert.equal(c.passiveRunning, true);
  assert.ok(events.some((e) => e.type === "control:passive_state" && e.detail.state === "running"));
  c._route(frame("off", "idle_wait"));
  assert.equal(c.passive.detail, "Wartet auf Leerlauf");
  assert.equal(c.passiveRunning, false);
});

ok("entering passive mutes RX streams, never the operator mic", () => {
  const a = makeAudio();
  a._onPassiveState(frame("running"));
  assert.equal(a.passiveActive, true);
  assert.equal(a.mixer.s1.muted, true);
  assert.equal(a.mixer.s2.muted, true);
  assert.equal(a.mixer["op.mic"].muted, false);
});

ok("leaving passive restores the passive-imposed mute", () => {
  const a = makeAudio();
  a._onPassiveState(frame("running"));
  a._onPassiveState(frame("off"));
  assert.equal(a.passiveActive, false);
  assert.equal(a.mixer.s1.muted, false);
  assert.equal(a.mixer.s2.muted, false);
});

ok("a stream the operator had muted stays muted after passive", () => {
  const a = makeAudio();
  a.mixer.s1.muted = true;
  a._onPassiveState(frame("running"));
  a._onPassiveState(frame("off"));
  assert.equal(a.mixer.s1.muted, true);
  assert.equal(a.mixer.s2.muted, false);
});

ok("user unmute during passive is respected, also when passive ends", () => {
  const a = makeAudio();
  a._onPassiveState(frame("running"));
  a.toggleMute("s1");
  assert.equal(a.mixer.s1.muted, false);
  assert.equal(a.passiveUserUnmuted, true);
  a._onPassiveState(frame("running")); // repeated frame: no re-mute
  assert.equal(a.mixer.s1.muted, false);
  a.toggleMute("s1"); // the user mutes again by hand ...
  a._onPassiveState(frame("off")); // ... and the end of the run leaves it alone
  assert.equal(a.mixer.s1.muted, true);
});

ok("a new run after a user unmute starts muted again", () => {
  const a = makeAudio();
  a._onPassiveState(frame("running"));
  a.passiveListenIn();
  assert.equal(a.mixer.s1.muted, false);
  a._onPassiveState(frame("off"));
  a._onPassiveState(frame("running"));
  assert.equal(a.mixer.s1.muted, true);
});

ok("passiveListenIn unmutes the RX streams and keeps them unmuted", () => {
  const a = makeAudio();
  a._onPassiveState(frame("running"));
  a.passiveListenIn();
  assert.equal(a.mixer.s1.muted, false);
  assert.equal(a.mixer.s2.muted, false);
  a._onPassiveState(frame("running"));
  assert.equal(a.mixer.s1.muted, false);
});

ok("applies the muted gain to a live stream", () => {
  const a = makeAudio();
  const gain = { gain: { value: 1 } };
  a._streamCtx.s1 = { gainNode: gain };
  a._onPassiveState(frame("running"));
  assert.equal(gain.gain.value, 0);
  a._onPassiveState(frame("off"));
  assert.equal(gain.gain.value, 1);
});

ok("a (re)opened socket resets a stale passive state", () => {
  const c = factories.controlPanel();
  c._route(frame("running"));
  c._subscribeAll = () => {};
  c._publishControlStore = () => {};
  const handlers = {};
  globalThis.WebSocket = class {
    constructor() {
      this.addEventListener = (n, fn) => (handlers[n] = fn);
    }
  };
  globalThis.WebSocket.OPEN = 1;
  c._wsUrl = () => "ws://x";
  c._connect();
  handlers.open();
  assert.equal(c.passiveRunning, false);
  assert.equal(c.passive.level, "off");
});

console.log("\n" + passed + " assertions passed");
