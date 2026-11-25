// The audio panel blocks the microphone while an SSTV picture is on the air
// (window event "sstv:tx" from static/js/sstv-panel.js).
// Run: node tests/js/audio-panel-sstv.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));

const listeners = {};
globalThis.window = new EventTarget();
globalThis.document = {
  addEventListener: (n, fn) => {
    listeners[n] = fn;
  },
};
window.OE5XRXAudioLogic = require(path.resolve(here, "../../static/js/audio-logic.js"));
require(path.resolve(here, "../../static/js/audio-panel.js"));
let factory = null;
window.Alpine = {
  data: (name, fn) => {
    if (name === "audioPanel") factory = fn;
  },
};
listeners["alpine:init"]();

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

ok("an SSTV transmission turns the mic off and keeps it off", () => {
  const c = factory();
  c._installListeners();
  let disabled = 0;
  c.disableMic = () => {
    disabled += 1;
    c.micEnabled = false;
  };
  c.micEnabled = true;
  window.dispatchEvent(new CustomEvent("sstv:tx", { detail: { active: true } }));
  assert.equal(c.sstvTx, true);
  assert.equal(disabled, 1);
  assert.equal(c.micEnabled, false);
  c.enableMic();
  assert.equal(c.micEnabled, false, "enableMic is a no-op while SSTV is on the air");
  window.dispatchEvent(new CustomEvent("sstv:tx", { detail: { active: false } }));
  assert.equal(c.sstvTx, false);
});

console.log(`# ${passed} passed`);
