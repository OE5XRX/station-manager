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

// Error-frame normalisation (spec 2026-10-14 §5.2): the permission frame is
// {type:"error", code:"forbidden", message}, the legacy one {error:{code,msg}}.
ok("forbidden frame for a widget request sets the widget error + deny notice", () => {
  const { c } = make(false);
  c._reqWidget["r1"] = "slot0 fm filter_hpf";
  c._onError({ type: "error", code: "forbidden", message: "Keine Berechtigung", request_id: "r1" });
  assert.equal(c.errors["slot0 fm filter_hpf"], "Not permitted");
  assert.equal(c.denyNotice, "Keine Berechtigung");
  assert.equal(c.connError, null);
  clearTimeout(c._denyTimer);
});
ok("legacy {error:{code}} frame still maps to the widget", () => {
  const { c } = make(false);
  c._reqWidget["r2"] = "slot0 fm frequency";
  c._onError({ type: "error", request_id: "r2", error: { code: "not_locked", msg: "x" } });
  assert.equal(c.errors["slot0 fm frequency"], "You don\u2019t have control");
  assert.equal(c.denyNotice, null);
});
ok("action denial without widget is a toast, not a connection error", () => {
  const { c } = make(false);
  c._onError({ type: "error", code: "forbidden", message: "Nope" });
  assert.equal(c.denyNotice, "Nope");
  assert.equal(c.connError, null);
  clearTimeout(c._denyTimer);
});
ok("connect-gate denial followed by 4403 becomes the connection error", () => {
  const { c } = make(false);
  c._onError({ type: "error", code: "forbidden", message: "Gate says no" });
  clearTimeout(c._denyTimer);
  c._closeCode = 4403;
  c._scheduleReconnect();
  assert.equal(c.conn, "closed");
  assert.equal(c.connError, "Gate says no");
  assert.equal(c.denyNotice, null);
});
ok("legacy reason frame still sets connError", () => {
  const { c } = make(false);
  c._onError({ type: "error", code: 4404, reason: "Station not found" });
  assert.equal(c.connError, "Station not found");
});

ok("a successful (re)connect clears a stale denial", () => {
  const { c } = make(false);
  c._onError({ type: "error", code: "forbidden", message: "Old denial" });
  clearTimeout(c._denyTimer);
  const handlers = {};
  globalThis.location = { protocol: "http:", host: "x" };
  globalThis.WebSocket = function () {
    this.addEventListener = (t, fn) => {
      handlers[t] = fn;
    };
  };
  c._subscribeAll = () => {};
  c._publishControlStore = () => {};
  c._connect();
  handlers.open();
  assert.equal(c._lastDenial, null);
  // A later 4403 without a fresh denial frame falls back to the generic text.
  c._closeCode = 4403;
  c._scheduleReconnect();
  assert.notEqual(c.connError, "Old denial");
});

console.log("\n" + passed + " assertions passed");
