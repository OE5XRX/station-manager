// Behavioural test: a data-readonly widget never sends a command.
// Loads static/js/control-panel.js against minimal window/document/Alpine stubs.
// Run: node tests/js/control-panel-readonly.test.mjs  (exit 0 = pass).

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
window.OE5XRXControlLogic = require(path.resolve(here, "../../static/js/control-logic.js"));
require(path.resolve(here, "../../static/js/control-panel.js"));

let factory = null;
window.Alpine = {
  store: () => ({}),
  data: (name, fn) => {
    if (name === "controlPanel") factory = fn;
  },
};
listeners["alpine:init"]();
assert.ok(factory, "controlPanel registered");

function make(readonly) {
  const c = factory();
  const sent = [];
  c._isOpen = () => true;
  c._send = (obj) => {
    sent.push(obj);
    return true;
  };
  c._widgetEl = () => ({
    getAttribute: (a) => (a === "data-readonly" && readonly ? "true" : null),
  });
  c._capType = () => "bool";
  return { c, sent };
}

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

ok("setValue sends nothing from a read-only widget", () => {
  const { c, sent } = make(true);
  c.setValue("slot0", "fm", "filter_hpf", true);
  assert.equal(sent.length, 0);
});
ok("doAction sends nothing from a read-only widget", () => {
  const { c, sent } = make(true);
  c.doAction("slot0", "fm", "reset");
  assert.equal(sent.length, 0);
});
ok("stepValue sends nothing from a read-only widget", () => {
  const { c, sent } = make(true);
  c._capType = () => "int";
  c._bounds = () => ({ step: 1, min: 0, max: 10 });
  c.values["slot0 fm freq"] = 5;
  c.stepValue("slot0", "fm", "freq", 1);
  assert.equal(sent.length, 0);
});
ok("control: setValue/doAction DO send from a writable widget", () => {
  const { c, sent } = make(false);
  c.setValue("slot0", "fm", "filter_hpf", true);
  c.doAction("slot0", "fm", "reset");
  assert.equal(sent.length, 2);
});

console.log("\n" + passed + " assertions passed");
