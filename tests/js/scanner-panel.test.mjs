// Scanner panel (static/js/scanner-panel.js) + the control-panel scan bridge.
// Run: node tests/js/scanner-panel.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));

const docListeners = {};
globalThis.window = new EventTarget();
globalThis.document = {
  addEventListener: (n, fn) => {
    (docListeners[n] = docListeners[n] || []).push(fn);
  },
  getElementById: () => null,
};
const store = { connected: true, youHold: true, canControl: true };
const registered = {};
window.Alpine = {
  store: () => store,
  data: (name, fn) => {
    registered[name] = fn;
  },
};
window.OE5XRXControlLogic = require(path.resolve(here, "../../static/js/control-logic.js"));
require(path.resolve(here, "../../static/js/control-panel.js"));
const { logic, scannerPanel } = require(path.resolve(here, "../../static/js/scanner-panel.js"));
docListeners["alpine:init"].forEach((fn) => fn());
assert.equal(typeof registered.scannerPanel, "function");

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

function memStorage(data = {}) {
  return { getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => (data[k] = String(v)) };
}

const CARRIER = [{ name: "carrier", kind: "telemetry" }];
const INV = [
  { slot: "1", modules: [{ module: "fm0", online: true, capabilities: CARRIER }] },
  { slot: "2", modules: [{ module: "fm1", online: false, capabilities: CARRIER }] },
  { slot: "3", modules: [{ module: "pb", online: true, capabilities: [{ name: "ptt" }] }] },
];

function panel(opts = {}) {
  const c = scannerPanel();
  c.stationId = "42";
  c._storage = () => memStorage(opts.storage || {});
  c.slots = opts.slots === undefined ? INV : opts.slots;
  c.lists = [
    { id: 3, name: "Home", count: 4 },
    { id: 5, name: "Trip", count: 2 },
  ];
  return c;
}

function capture(type) {
  const seen = [];
  const h = (e) => seen.push(e.detail);
  window.addEventListener(type, h);
  return { seen, stop: () => window.removeEventListener(type, h) };
}

// -- visibility ----------------------------------------------------------------
ok("hidden unless an online module advertises carrier", () => {
  assert.equal(logic.carrierSlots(INV).join(), "1");
  assert.equal(logic.carrierSlots([INV[1], INV[2]]).length, 0);
  assert.equal(panel().visible, true);
  assert.equal(panel({ slots: [INV[1], INV[2]] }).visible, false);
  assert.equal(panel({ slots: [] }).visible, false);
});

// -- start frame -----------------------------------------------------------------
ok("start frame carries radius, lists, band_map and timing", () => {
  const c = panel({ storage: { "oe5xrx.bandmap.42": '{"vhf":"1"}' } });
  c.useRadius = true;
  c.radiusKm = "50";
  c.selected = ["3", "5"];
  c.listenS = "2,5"; // de_AT comma decimal
  c.hangS = "5";
  const cap = capture("control:send");
  assert.equal(c.start(), true);
  cap.stop();
  const f = cap.seen[0];
  assert.equal(f.type, "scan_start");
  assert.match(f.request_id, /^sc-/);
  assert.equal(f.radius_km, 50);
  assert.deepEqual(f.list_ids, [3, 5]);
  assert.deepEqual(f.band_map, { vhf: "1" });
  assert.equal(f.listen_s, 2.5);
  assert.equal(f.hang_s, 5);
});

ok("radius off sends null; nothing selected is refused locally", () => {
  const c = panel();
  const cap = capture("control:send");
  assert.equal(c.start(), false);
  assert.equal(cap.seen.length, 0);
  assert.ok(c.notice && !c.notice.ok);
  c.selected = ["3"];
  assert.equal(c.start(), true);
  cap.stop();
  assert.equal(cap.seen[0].radius_km, null);
});

ok("timing is clamped to the agent's ranges; bad radius refused", () => {
  assert.equal(logic.clamp("0", 0.2, 60, 2), 0.2);
  assert.equal(logic.clamp("999", 0, 600, 5), 600);
  assert.equal(logic.clamp("abc", 0, 600, 5), 5);
  const c = panel();
  c.useRadius = true;
  c.radiusKm = "-3";
  assert.equal(c.start(), false);
});

ok("start needs the lock", () => {
  const c = panel();
  c.selected = ["3"];
  store.youHold = false;
  assert.equal(c.canUse, false);
  assert.equal(c.start(), false);
  store.youHold = true;
});

// -- state rendering -----------------------------------------------------------------
ok("scan_state drives the state line", () => {
  const c = panel();
  c._onScanState({ state: "scanning", index: 1, ref: "r:7", slot: "1", module: "fm0", label: "OE5XLL", rx_frequency: 145.6, count: 6 });
  assert.equal(c.running, true);
  assert.equal(c.stateLabel(), "Scanning");
  assert.equal(c.position(), "2 / 6");
  assert.equal(c.where(), "OE5XLL · 145.6000 MHz · fm0");
  c._onScanState({ state: "active", index: 1, ref: "r:7", slot: "1", module: "fm0" });
  assert.equal(c.stateLabel(), "Carrier");
  c._onScanState({ state: "hang", index: 1, ref: "r:7", slot: "1", module: "fm0" });
  assert.equal(c.stateLabel(), "Waiting for reply");
  c._onScanState({ state: "held", index: 1, ref: "r:7", slot: null, module: null, reason: "ptt" });
  assert.equal(c.held, true);
  assert.match(c.stateLabel(), /Held/);
  assert.match(c.reasonText("ptt"), /transmit/i);
});

ok("stopped clears the scan and explains the reason", () => {
  const c = panel();
  c._onScanState({ state: "scanning", index: 0, ref: "r:1", slot: "1", module: "fm0" });
  c._onScanState({ state: "stopped", reason: "no_reachable_entries" });
  assert.equal(c.running, false);
  assert.ok(c.notice && !c.notice.ok);
  for (const r of ["operator", "replaced", "disconnect", "shutdown", "module_update", "empty_scan_list", "invalid_scan", "internal_error"]) {
    assert.ok(logic.reasonText(r).length > 5, r);
  }
  assert.equal(c.where(), "");
});

// -- controls -------------------------------------------------------------------------
ok("buttons send scan_control actions", () => {
  const c = panel();
  c._onScanState({ state: "scanning", index: 0, ref: "r:1", slot: "1", module: "fm0" });
  const cap = capture("control:send");
  for (const a of ["stop", "skip", "hold", "resume"]) assert.equal(c.control(a), true);
  cap.stop();
  assert.deepEqual(cap.seen.map((f) => [f.type, f.action]), [
    ["scan_control", "stop"], ["scan_control", "skip"], ["scan_control", "hold"], ["scan_control", "resume"],
  ]);
  assert.ok(cap.seen.every((f) => /^sc-/.test(f.request_id)));
  assert.equal(c.control("bogus"), false);
});

ok("hold/resume buttons follow the held state", () => {
  const c = panel();
  c._onScanState({ state: "scanning", index: 0, ref: "r:1", slot: "1", module: "fm0" });
  assert.equal(c.canHold, true);
  assert.equal(c.canResume, false);
  c._onScanState({ state: "held", index: 0, ref: "r:1", slot: "1", module: "fm0", reason: "manual" });
  assert.equal(c.canHold, false);
  assert.equal(c.canResume, true);
});

ok("replies: errors map to texts, matching only our request ids", () => {
  const c = panel();
  c.selected = ["3"];
  const cap = capture("control:send");
  c.start();
  cap.stop();
  const rid = cap.seen[0].request_id;
  c._onReply({ type: "error", request_id: "other", error: { code: "forbidden" } });
  assert.equal(c.pending, true);
  c._onReply({ type: "error", request_id: rid, error: { code: "empty_scan_list" } });
  assert.equal(c.pending, false);
  assert.match(c.notice.text, /no repeater|empty|nothing/i);
  for (const code of ["not_locked", "forbidden", "no_coordinates", "empty_scan_list", "not_connected", "invalid_scan", "invalid_action", "no_active_scan"]) {
    assert.ok(logic.errorText(code).length > 5, code);
  }
});

// -- bridge into the control panel ----------------------------------------------------
ok("control panel bridges scan_start/scan_control and re-emits scan_state", () => {
  const cp = registered.controlPanel();
  const sent = [];
  cp._send = (f) => (sent.push(f), true);
  cp._bridgeReqs = {};
  cp._onSendEvent({ detail: { type: "scan_start", request_id: "a" } });
  cp._onSendEvent({ detail: { type: "scan_control", request_id: "b", action: "stop" } });
  cp._onSendEvent({ detail: { type: "unlock_everything", request_id: "c" } });
  assert.deepEqual(sent.map((f) => f.type), ["scan_start", "scan_control"]);
  const cap = capture("control:scan_state");
  cp._route({ type: "scan_state", state: "scanning" });
  cap.stop();
  assert.equal(cap.seen.length, 1);
});

ok("scan audio hook lists the carrier slots", () => {
  const c = panel();
  const cap = capture("scan:audio");
  c._onScanState({ state: "scanning", index: 0, ref: "r:1", slot: "1", module: "fm0" });
  c._onScanState({ state: "active", index: 0, ref: "r:1", slot: "1", module: "fm0" });
  cap.stop();
  assert.equal(cap.seen.length, 1); // once per scan, not per transition
  assert.deepEqual(cap.seen[0].slots, ["1"]);
});

ok("scan_state settles the in-flight request (no stuck buttons, no timeout notice)", () => {
  const c = panel();
  c.selected = ["3"];
  c.start();
  assert.equal(c.pending, true);
  c._onScanState({ state: "scanning", index: 0, ref: "r:1", slot: "1", module: "fm0" });
  assert.equal(c.pending, false);
  assert.deepEqual(c._requests, {});
  assert.equal(c.notice, null);
  c.control("stop");
  assert.equal(c.pending, true);
  c._onScanState({ state: "stopped", reason: "operator" });
  assert.equal(c.pending, false);
});

ok("a lost reply times out with a notice and unblocks", () => {
  const c = panel();
  c.selected = ["3"];
  const cap = capture("control:send");
  c.start();
  cap.stop();
  c._onReply({ type: "error", request_id: cap.seen[0].request_id, error: { code: "timeout" } });
  assert.equal(c.pending, false);
  assert.match(c.notice.text, /did not answer/);
});

ok("init seeds slots from the inventory and follows control:inventory", () => {
  const c = scannerPanel();
  c.$el = { getAttribute: (k) => (k === "data-station-id" ? "42" : null) };
  const realGet = document.getElementById;
  document.getElementById = () => ({ textContent: JSON.stringify([INV[1]]) });
  c.init();
  document.getElementById = realGet;
  assert.equal(c.visible, false);
  window.dispatchEvent(new CustomEvent("control:inventory", { detail: { slots: INV } }));
  assert.equal(c.visible, true);
  c.destroy();
});

ok("scan stop tells the audio panel to let go", () => {
  const c = panel();
  c._onScanState({ state: "scanning", index: 0, ref: "r:1", slot: "1", module: "fm0" });
  const cap = capture("scan:audio");
  c._onScanState({ state: "stopped", reason: "operator" });
  cap.stop();
  assert.deepEqual(cap.seen[0].slots, []);
});

console.log(`${passed} passed`);
