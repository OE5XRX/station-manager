// Repeater panel (static/js/repeater-panel.js) + the control-panel "control:send" bridge.
// Run: node tests/js/repeater-panel.test.mjs  (exit 0 = pass).

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
const { logic, repeaterPanel } = require(path.resolve(here, "../../static/js/repeater-panel.js"));
docListeners["alpine:init"].forEach((fn) => fn());
assert.equal(typeof registered.controlPanel, "function");
assert.equal(typeof registered.repeaterPanel, "function");

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

function memStorage() {
  const data = {};
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = String(v);
    },
  };
}

const VHF2 = {
  vhf: [
    { slot: "1", module: "fm0" },
    { slot: "2", module: "fm1" },
  ],
  uhf: [{ slot: "2", module: "fm1" }],
};

const ROW = {
  id: 7,
  callsign: "OE5XLL",
  output_frequency: "145.60000",
  input_frequency: "145.00000",
  input_tone: "77.0",
  output_tone: "none",
  distance_km: 9.3,
  bearing: "NE",
  rx_band: "vhf",
  tx_band: "vhf",
  cross_band: false,
};
const CROSS = { ...ROW, id: 8, callsign: "OE5CB", output_frequency: "438.65", rx_band: "uhf", cross_band: true };

function panel(storage) {
  const c = repeaterPanel();
  c.stationId = "42";
  c.bands = VHF2;
  c._storage = () => storage || memStorage();
  return c;
}

function capture(type) {
  const seen = [];
  const h = (e) => seen.push(e.detail);
  window.addEventListener(type, h);
  return { seen, stop: () => window.removeEventListener(type, h) };
}

// -- pure helpers ------------------------------------------------------------
ok("bandmap persists per station under oe5xrx.bandmap.<id>", () => {
  const s = memStorage();
  assert.equal(logic.saveBandmap(s, 42, { vhf: "2" }), true);
  assert.equal(s.data["oe5xrx.bandmap.42"], '{"vhf":"2"}');
  assert.deepEqual(logic.loadBandmap(s, 42), { vhf: "2" });
  assert.deepEqual(logic.loadBandmap(s, 43), {});
});

ok("corrupt or hostile bandmap storage falls back to empty", () => {
  const s = memStorage();
  s.data["oe5xrx.bandmap.1"] = "{not json";
  assert.deepEqual(logic.loadBandmap(s, 1), {});
  s.data["oe5xrx.bandmap.1"] = '["x"]';
  assert.deepEqual(logic.loadBandmap(s, 1), {});
  s.data["oe5xrx.bandmap.1"] = '{"vhf":3,"uhf":"2"}';
  assert.deepEqual(logic.loadBandmap(s, 1), { uhf: "2" });
  const throwing = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("quota");
    },
  };
  assert.deepEqual(logic.loadBandmap(throwing, 1), {});
  assert.equal(logic.saveBandmap(throwing, 1, {}), false);
});

ok("dropdown only for bands with more than one module", () => {
  assert.deepEqual(logic.multiBands(VHF2), ["vhf"]);
  assert.deepEqual(logic.multiBands({ vhf: [{ slot: "1", module: "fm0" }] }), []);
  assert.deepEqual(logic.multiBands({}), []);
});

ok("resolveSlot uses the remembered module only while it still serves the band", () => {
  assert.equal(logic.resolveSlot(VHF2, {}, "vhf"), "1");
  assert.equal(logic.resolveSlot(VHF2, { vhf: "2" }, "vhf"), "2");
  assert.equal(logic.resolveSlot(VHF2, { vhf: "9" }, "vhf"), "1");
  assert.equal(logic.resolveSlot(VHF2, {}, "23cm"), null);
});

ok("tuned row matches rx=output and tx=input", () => {
  assert.equal(logic.tunedSlot(ROW, { 1: { rx: 145.6, tx: 145.0 } }), "1");
  assert.equal(logic.tunedSlot(ROW, { 1: { rx: 145.6, tx: 145.6 } }), null);
  assert.equal(logic.tunedSlot(ROW, {}), null);
});

ok("state frames and the inventory snapshot feed the tuning map", () => {
  const t = logic.tuningFromInventory([
    { slot: "1", modules: [{ state: { rx_frequency: 145.6, tx_frequency: 145.0 } }] },
    { slot: "3", modules: [{ state: { volume: 4 } }] },
  ]);
  assert.deepEqual(t, { 1: { rx: 145.6, tx: 145.0 } });
  logic.applyStateFrame(t, { slot: 1, values: { tx_frequency: 144.8 } });
  assert.deepEqual(t["1"], { rx: 145.6, tx: 144.8 });
  logic.applyStateFrame(t, { slot: 2, values: { rssi: 3 } });
  assert.equal(t["2"], undefined);
});

ok("formatting: MHz, shift, tone, distance", () => {
  assert.equal(logic.formatMHz("145.60000"), "145.6000");
  assert.equal(logic.formatShift(ROW), "−600 kHz");
  assert.equal(logic.formatShift({ output_frequency: "438.65", input_frequency: "431.05" }), "−7.6 MHz");
  assert.equal(logic.formatShift({ output_frequency: "145.5", input_frequency: "145.5" }), "simplex");
  assert.equal(logic.formatTone("none"), "");
  assert.equal(logic.formatTone("023"), "D023");
  assert.equal(logic.formatTone("77.0"), "77.0");
  assert.equal(logic.formatDistance(ROW), "9.3 km NE");
  assert.equal(logic.formatDistance({ distance_km: 23.4, bearing: "S" }), "23 km S");
  assert.equal(logic.formatDistance({ distance_km: null }), "");
});

ok("result summary names the caps that failed", () => {
  assert.deepEqual(logic.resultText({ ok: true }, "OE5XLL"), { ok: true, text: "Tuned to OE5XLL." });
  const partial = logic.resultText(
    { ok: false, error: { code: "preset_failed" }, value: { tx_tone: { ok: false }, tx_frequency: { ok: true } } },
    "OE5XLL",
  );
  assert.equal(partial.ok, false);
  assert.match(partial.text, /tx tone/);
  assert.doesNotMatch(partial.text, /tx frequency/);
  assert.match(logic.resultText({ ok: false, error: { code: "ptt_active" } }, "X").text, /PTT/);
});

// -- component ----------------------------------------------------------------
ok("chooseSlot stores the band choice and redirects clicks", () => {
  const s = memStorage();
  const c = panel(s);
  assert.deepEqual(c.mappedBands, ["vhf"]);
  c.chooseSlot("vhf", "2");
  assert.equal(s.data["oe5xrx.bandmap.42"], '{"vhf":"2"}');
  const cap = capture("control:send");
  assert.equal(c.apply(ROW), true);
  cap.stop();
  assert.equal(cap.seen.length, 1);
  const frame = cap.seen[0];
  assert.equal(frame.type, "apply_repeater");
  assert.equal(frame.repeater_id, 7);
  assert.equal(frame.slot, "2");
  assert.equal(typeof frame.request_id, "string");
});

ok("cross-band rows are not clickable", () => {
  const c = panel();
  assert.equal(c.rowDisabled(CROSS), true);
  assert.match(c.rowTitle(CROSS), /not yet supported/);
  const cap = capture("control:send");
  assert.equal(c.apply(CROSS), false);
  cap.stop();
  assert.equal(cap.seen.length, 0);
});

ok("rows are inert without the lock", () => {
  const c = panel();
  store.youHold = false;
  try {
    assert.equal(c.canApply, false);
    const cap = capture("control:send");
    assert.equal(c.apply(ROW), false);
    cap.stop();
    assert.equal(cap.seen.length, 0);
  } finally {
    store.youHold = true;
  }
});

ok("one apply at a time; the matching reply clears pending and notifies", () => {
  const c = panel();
  const cap = capture("control:send");
  c.apply(ROW);
  assert.equal(c.isPending(ROW), true);
  assert.equal(c.apply({ ...ROW, id: 9 }), false); // blocked while pending
  cap.stop();
  const rid = cap.seen[0].request_id;
  c._onReply({ type: "result", request_id: "someone-else", ok: true });
  assert.equal(c.isPending(ROW), true);
  c._onReply({ type: "result", request_id: rid, ok: true });
  assert.equal(c.pendingId, null);
  assert.deepEqual(c.notice, { ok: true, text: "Tuned to OE5XLL." });
  c.destroy();
});

ok("an error reply surfaces the server's reason", () => {
  const c = panel();
  const cap = capture("control:send");
  c.apply(ROW);
  cap.stop();
  c._onReply({ type: "error", request_id: cap.seen[0].request_id, error: { code: "not_locked" } });
  assert.equal(c.notice.ok, false);
  assert.match(c.notice.text, /control/);
  c._onReply({ type: "error", request_id: "x", code: "forbidden" }); // not ours: ignored
  c.destroy();
});

ok("isTuned highlights from the live tuning map", () => {
  const c = panel();
  c.tuning = { 1: { rx: 145.6, tx: 145.0 } };
  assert.equal(c.isTuned(ROW), true);
  assert.equal(c.isTuned(CROSS), false);
});

// -- control-panel bridge -------------------------------------------------------
ok("control panel forwards control:send frames to its socket", () => {
  const cp = registered.controlPanel();
  const sent = [];
  cp._send = (f) => {
    sent.push(f);
    return true;
  };
  cp._onSendEvent({ detail: { type: "apply_repeater", request_id: "r1", repeater_id: 1, slot: "1" } });
  cp._onSendEvent({ detail: "junk" });
  cp._onSendEvent({ detail: { request_id: "r2" } });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, "apply_repeater");
});

ok("an unsendable frame is answered with not_connected", () => {
  const cp = registered.controlPanel();
  cp._send = () => false;
  const cap = capture("control:error");
  cp._onSendEvent({ detail: { type: "apply_repeater", request_id: "r3" } });
  cap.stop();
  assert.equal(cap.seen.length, 1);
  assert.equal(cap.seen[0].request_id, "r3");
  assert.equal(cap.seen[0].error.code, "not_connected");
});

ok("control panel re-emits state/result/error/inventory frames as window events", () => {
  const cp = registered.controlPanel();
  cp._onState = () => {};
  cp._onResult = () => {};
  cp._ingestInventory = () => {};
  cp._subscribeAll = () => {};
  const caps = ["state", "result", "inventory"].map((t) => capture("control:" + t));
  cp._route({ type: "state", slot: 1, module: "fm0", values: {} });
  cp._route({ type: "result", request_id: "r1", ok: true });
  cp._route({ type: "inventory", slots: [] });
  cp._route({ type: "lock", state: "free" });
  caps.forEach((c) => c.stop());
  assert.deepEqual(
    caps.map((c) => c.seen.length),
    [1, 1, 1],
  );
});

console.log(`# ${passed} passed`);
// Pending give-up / notice timers would otherwise keep the event loop alive.
process.exit(0);
