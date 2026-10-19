// Behavioural test: module firmware update state in the control panel
// (Teilbereich D, Task S6). Loads static/js/control-panel.js against minimal
// window/document/Alpine stubs.
// Run: node tests/js/control-panel-firmware.test.mjs  (exit 0 = pass).

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
const L = require(path.resolve(here, "../../static/js/control-logic.js"));
window.OE5XRXControlLogic = L;
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

const CAPS = [
  { name: "frequency", kind: "setting", type: "float" },
  { name: "ptt", kind: "action", type: "bool" },
];

function inv(extra) {
  return [
    {
      slot: "1",
      modules: [Object.assign({ module: "fm", capabilities: CAPS, state: {} }, extra || {})],
    },
  ];
}

function make() {
  const c = factory();
  const sent = [];
  c._isOpen = () => true;
  c._send = (obj) => {
    sent.push(obj);
    return true;
  };
  c._widgetEl = () => ({ getAttribute: () => null });
  c._capType = () => "float";
  // Operating requires the lock + an open socket.
  c.youHold = true;
  c.conn = "open";
  return { c, sent };
}

function ev(event, detail) {
  return { type: "event", slot: "1", module: "fm", event, detail: detail || {} };
}

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

ok("inventory status=updating marks the module updating and disables it", () => {
  const { c } = make();
  c._ingestInventory(inv({ status: "updating" }));
  assert.equal(c.isUpdating("1", "fm"), true);
  assert.equal(c.moduleOnline("1", "fm"), true);
  assert.equal(c.canOperate("1", "fm"), false);
});
ok("snapshot updating=true marks the module updating", () => {
  const { c } = make();
  c._ingestInventory(inv({ updating: true, online: true }));
  assert.equal(c.isUpdating("1", "fm"), true);
});
ok("a normal inventory clears the updating state", () => {
  const { c } = make();
  c._ingestInventory(inv({ status: "updating" }));
  c._ingestInventory(inv());
  assert.equal(c.isUpdating("1", "fm"), false);
  assert.equal(c.canOperate("1", "fm"), true);
});
ok("firmware_update_started / finished events toggle the state", () => {
  const { c } = make();
  c._ingestInventory(inv());
  c._route(ev("firmware_update_started", { target_version: "2.0", convergence_id: 3 }));
  assert.equal(c.isUpdating("1", "fm"), true);
  assert.equal(c.canOperate("1", "fm"), false);
  c._route(ev("firmware_update_finished", { result: "success", target_version: "2.0" }));
  assert.equal(c.isUpdating("1", "fm"), false);
  assert.equal(c.canOperate("1", "fm"), true);
});
ok("an updating module sends no set/do/ptt commands", () => {
  const { c, sent } = make();
  c._ingestInventory(inv({ status: "updating" }));
  c.setValue("1", "fm", "frequency", "145,5");
  c.doAction("1", "fm", "ptt");
  c.pttDown("1", "fm");
  assert.equal(sent.length, 0);
});
ok("firmware_update_started releases a local PTT key", () => {
  const { c } = make();
  c._ingestInventory(inv());
  c.ptt["1 fm"] = "tx";
  c._route(ev("firmware_update_started", {}));
  assert.equal(L.isKeyed(c.ptt["1 fm"]), false);
});
ok("firmware_dfu_stuck sets the module error from detail.msg", () => {
  const { c } = make();
  c._ingestInventory(inv());
  c._route(ev("firmware_dfu_stuck", { msg: "Modul hängt im DFU-Modus — Power-Cycle nötig" }));
  assert.equal(c.errors["1 fm"], "Modul hängt im DFU-Modus — Power-Cycle nötig");
  assert.equal(c.moduleError("1", "fm"), "Modul hängt im DFU-Modus — Power-Cycle nötig");
});
ok("a new attempt / a successful finish clears the module error", () => {
  const { c } = make();
  c._ingestInventory(inv());
  c._route(ev("firmware_dfu_stuck", { msg: "stuck" }));
  c._route(ev("firmware_update_started", {}));
  assert.equal(c.moduleError("1", "fm"), "");
  c._route(ev("firmware_dfu_stuck", { msg: "stuck" }));
  c._route(ev("firmware_update_finished", { result: "failed" }));
  assert.equal(c.moduleError("1", "fm"), "stuck");
  c._route(ev("firmware_update_finished", { result: "success" }));
  assert.equal(c.moduleError("1", "fm"), "");
});
ok("firmware_update_pending error shows the message as a notice", () => {
  const { c } = make();
  c._route({
    type: "error",
    code: "firmware_update_pending",
    message: "Firmware-Update ausstehend — neue Steuerung ist gesperrt.",
  });
  assert.equal(c.denyNotice, "Firmware-Update ausstehend — neue Steuerung ist gesperrt.");
  assert.equal(c.connError, null);
  clearTimeout(c._denyTimer);
});
ok("lock frame drain.since drives the station drain banner", () => {
  const { c } = make();
  const since = "2026-10-18T08:05:00+00:00";
  c._route({ type: "lock", state: "free", you_hold: false, drain: { since } });
  assert.equal(c.drainSince, since);
  assert.equal(c.drainText(), "Station wartet auf Firmware-Update seit " + L.formatLocalDateTime(since));
  c._route({ type: "lock", state: "free", you_hold: false, drain: null });
  assert.equal(c.drainSince, null);
  assert.equal(c.drainText(), "");
});
ok("init seeds drainSince from the server-rendered data attribute", () => {
  const c = factory();
  c._seedFromInitial = () => {};
  c._installListeners = () => {};
  c._connect = () => {};
  const attrs = {
    "data-station-id": "5",
    "data-can-admin": "0",
    "data-drain-since": "2026-10-18T08:05:00+00:00",
  };
  c.$el = { getAttribute: (a) => (a in attrs ? attrs[a] : null) };
  globalThis.localStorage = { getItem: () => null };
  c.init();
  assert.equal(c.drainSince, "2026-10-18T08:05:00+00:00");
  attrs["data-drain-since"] = "";
  const d = factory();
  d._seedFromInitial = () => {};
  d._installListeners = () => {};
  d._connect = () => {};
  d.$el = c.$el;
  d.init();
  assert.equal(d.drainSince, null);
});

console.log("\n" + passed + " assertions passed");
