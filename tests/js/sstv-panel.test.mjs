// SSTV section component (static/js/sstv-panel.js) + the control-panel bridge,
// the voice-PTT block and the audio panel's mic block during a transmission.
// Run: node tests/js/sstv-panel.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));

const docListeners = {};
const bodyEvents = [];
globalThis.window = new EventTarget();
globalThis.document = {
  addEventListener: (n, fn) => {
    (docListeners[n] = docListeners[n] || []).push(fn);
  },
  getElementById: () => null,
  body: { dispatchEvent: (e) => bodyEvents.push(e.type) },
};
const commands = [];
const store = {
  connected: true,
  youHold: true,
  canControl: true,
  sendCommand: (f) => (commands.push(f), true),
};
const registered = {};
window.Alpine = {
  store: () => store,
  data: (name, fn) => {
    registered[name] = fn;
  },
};
window.OE5XRXControlLogic = require(path.resolve(here, "../../static/js/control-logic.js"));
window.OE5XRXSstvLogic = require(path.resolve(here, "../../static/js/sstv-logic.js"));
require(path.resolve(here, "../../static/js/control-panel.js"));
const { sstvPanel } = require(path.resolve(here, "../../static/js/sstv-panel.js"));
docListeners["alpine:init"].forEach((fn) => fn());
assert.equal(typeof registered.sstvPanel, "function");

let passed = 0;
async function ok(name, fn) {
  await fn();
  passed += 1;
  console.log("ok - " + name);
}

const FM = [{ name: "rx_frequency" }, { name: "tx_frequency" }, { name: "ptt" }];
const INV = [
  { slot: "1", modules: [{ module: "fm0", online: true, capabilities: FM, state: { rx_frequency: 145.5, tx_frequency: 145.5 } }] },
  { slot: "2", modules: [{ module: "rx1", online: true, capabilities: [{ name: "rx_frequency" }], state: { rx_frequency: 433.5 } }] },
];

function fakeRoot(attrs = {}) {
  const dialog = {
    shown: 0,
    closed: 0,
    showModal() {
      this.shown += 1;
    },
    close() {
      this.closed += 1;
    },
  };
  const canvasCalls = [];
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => ({
      clearRect: () => canvasCalls.push("clear"),
      putImageData: (img, x, y) => canvasCalls.push(["row", y, img.width]),
      drawImage: () => canvasCalls.push("draw"),
    }),
  };
  const values = Object.assign(
    { "data-station-id": "7", "data-preview-url": "/en/stations/7/sstv/preview/", "data-can-use": "1", "data-station-callsign": "1" },
    attrs
  );
  return {
    dialog,
    canvas,
    canvasCalls,
    getAttribute: (k) => (k in values ? values[k] : null),
    querySelector: (sel) => {
      if (sel === "[data-sv=confirm]") return dialog;
      if (sel === "[data-sv=canvas]") return canvas;
      if (sel === "input[name=csrfmiddlewaretoken]") return { value: "tok" };
      return null;
    },
  };
}

function panel(attrs) {
  const c = sstvPanel();
  const root = fakeRoot(attrs);
  c.$el = root;
  c.init();
  c._seed(INV);
  c.root = root;
  return c;
}

function capture(type) {
  const seen = [];
  const h = (e) => seen.push(e.detail);
  window.addEventListener(type, h);
  return { seen, stop: () => window.removeEventListener(type, h) };
}

globalThis.ImageData = class {
  constructor(data, width, height) {
    this.data = data;
    this.width = width;
    this.height = height;
  }
};

// -- receive ----------------------------------------------------------------------
await ok("start/stop receive go out through the bridge, state settles them", () => {
  const c = panel();
  assert.equal(c.selected, "1 fm0");
  const cap = capture("control:send");
  assert.equal(c.startRx(), true);
  assert.equal(cap.seen[0].type, "sstv_rx_start");
  assert.equal(cap.seen[0].slot, "1");
  assert.ok(cap.seen[0].request_id.startsWith("sv-"), "sstv- is reserved for the server");
  assert.equal(c.pending, true);
  window.dispatchEvent(new CustomEvent("control:sstv_state", { detail: { state: "running", slot: 1, ends_at: new Date(Date.now() + 1800000).toISOString() } }));
  assert.equal(c.pending, false);
  assert.equal(c.rxLive, true);
  assert.match(c.remaining(), /^(30|29):\d\d$/);
  assert.equal(c.startRx(), false);
  c.stopRx();
  assert.equal(cap.seen[1].type, "sstv_rx_stop");
  window.dispatchEvent(new CustomEvent("control:sstv_state", { detail: { state: "idle", detail: "operator" } }));
  assert.equal(c.rxLive, false);
  assert.equal(c.notice, null, "own stop needs no explanation");
  cap.stop();
  c.destroy();
});

await ok("receive ended by the server explains why", () => {
  const c = panel();
  c._onRxState({ state: "running", slot: 1, ends_at: null });
  c._onRxState({ state: "idle", detail: "max_runtime" });
  assert.equal(c.notice.ok, false);
  assert.match(c.notice.text, /30 minutes/);
  c.destroy();
});

await ok("no controls work without the lock or without sstv.use", () => {
  const c = panel();
  store.youHold = false;
  assert.equal(c.canStartRx, false);
  assert.equal(c.canIss, false);
  assert.equal(c.startRx(), false);
  assert.match(c.sendHint(), /Take control/);
  store.youHold = true;
  const v = panel({ "data-can-use": "0" });
  assert.equal(v.canOperate, false);
  assert.equal(v.canPreview, false);
  assert.equal(v.sendHint(), "");
  c.destroy();
  v.destroy();
});

await ok("ISS sets the RX frequency to 145.800 MHz with a normal command", () => {
  const c = panel();
  commands.length = 0;
  assert.equal(c.tuneIss(), true);
  assert.equal(commands.length, 1);
  assert.deepEqual(
    { type: commands[0].type, slot: commands[0].slot, module: commands[0].module, capability: commands[0].capability, op: commands[0].op, value: commands[0].value },
    { type: "command", slot: 1, module: "fm0", capability: "rx_frequency", op: "set", value: 145.8 }
  );
  window.dispatchEvent(new CustomEvent("control:state", { detail: { slot: 1, module: "fm0", values: { rx_frequency: 145.8 } } }));
  assert.equal(c.onIss, true);
  assert.equal(c.rxMHz(), "145.8000");
  assert.equal(c.canIss, false, "already there");
  // a refused command is reported
  c._onModuleState({ slot: 1, module: "fm0", values: { rx_frequency: 145.5 } });
  c.tuneIss();
  const rid = commands[1].request_id;
  window.dispatchEvent(new CustomEvent("control:result", { detail: { type: "result", request_id: rid, ok: false, error: { code: "bad_value" } } }));
  assert.equal(c.notice.ok, false);
  c.destroy();
});

await ok("live picture: canvas sized to the mode, rows painted, foreign rows ignored", () => {
  const c = panel();
  c._onPictureStart({ picture_id: "p1", mode: "robot36", width: 320, height: 240 });
  assert.equal(c.root.canvas.width, 320);
  assert.equal(c.root.canvas.height, 240);
  const rgb = Buffer.alloc(320 * 3, 50).toString("base64");
  window.dispatchEvent(new CustomEvent("control:sstv_line", { detail: { picture_id: "p1", row: 0, rgb } }));
  window.dispatchEvent(new CustomEvent("control:sstv_line", { detail: { picture_id: "p1", row: 7, rgb } }));
  window.dispatchEvent(new CustomEvent("control:sstv_line", { detail: { picture_id: "other", row: 8, rgb } }));
  window.dispatchEvent(new CustomEvent("control:sstv_line", { detail: { picture_id: "p1", row: 240, rgb } }));
  const rows = c.root.canvasCalls.filter((x) => Array.isArray(x));
  assert.deepEqual(rows, [["row", 0, 320], ["row", 7, 320]]);
  assert.equal(c.picture.row, 7);
  assert.match(c.pictureLabel(), /row 8 of 240/);
  c._onPictureStart({ picture_id: "p2", mode: "x", width: 99999, height: 1 });
  assert.equal(c.picture.id, "p1", "absurd sizes are ignored");
  c.destroy();
});

await ok("final picture refreshes the gallery", () => {
  const c = panel();
  bodyEvents.length = 0;
  window.dispatchEvent(new CustomEvent("control:sstv_image", { detail: { id: "u", url: "/en/stations/7/sstv/u/image/", direction: "rx", complete: false } }));
  assert.deepEqual(bodyEvents, ["sstv-gallery-refresh"]);
  assert.equal(c.lastImage.complete, false);
  c.destroy();
});

// -- send ---------------------------------------------------------------------------
function fakeFetch(status, body) {
  const calls = [];
  globalThis.fetch = (url, opts) => {
    calls.push({ url, opts });
    return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
  };
  return calls;
}
globalThis.FormData = class {
  constructor() {
    this.entries = {};
  }
  append(k, v) {
    this.entries[k] = v;
  }
};

await ok("preview posts the choices with CSRF and module", async () => {
  const c = panel();
  const calls = fakeFetch(200, { id: "abc", preview_url: "/p.png", duration_s: 127.4, mode: "pd120", slot: 1, module: "fm0", warnings: [] });
  assert.equal(await c.makePreview(), true);
  assert.equal(calls[0].url, "/en/stations/7/sstv/preview/");
  assert.equal(calls[0].opts.headers["X-CSRFToken"], "tok");
  const e = calls[0].opts.body.entries;
  assert.deepEqual([e.source, e.mode, e.cs_user, e.cs_station, e.slot, e.module], ["test", "pd120", "1", "1", "1", "fm0"]);
  assert.equal(c.preview.id, "abc");
  assert.equal(c.preview.noCallsign, false);
  assert.equal(c.canSend, true);
  c.invalidate();
  assert.equal(c.canSend, false, "a changed choice needs a new preview");
  c.destroy();
});

await ok("preview refusal shows the server's message", async () => {
  const c = panel();
  fakeFetch(400, { error: "too_large", message: "The file is larger than 15 MB." });
  assert.equal(await c.makePreview(), false);
  assert.equal(c.notice.text, "The file is larger than 15 MB.");
  assert.equal(c.preview, null);
  c.destroy();
});

await ok("station callsign is never requested when the station has none", async () => {
  const c = panel({ "data-station-callsign": "0" });
  assert.equal(c.csStation, false);
  c.csStation = true;
  const calls = fakeFetch(200, { id: "x", preview_url: "/p", duration_s: 1, mode: "pd120", slot: 1, module: "fm0", warnings: [] });
  await c.makePreview();
  assert.equal(calls[0].opts.body.entries.cs_station, "0");
  c.destroy();
});

await ok("no callsign: send asks first, confirm sends with confirm_no_callsign", () => {
  const c = panel();
  c.preview = { id: "abc", url: "/p", duration_s: 1, mode: "pd120", slot: 1, module: "fm0", noCallsign: true };
  const cap = capture("control:send");
  assert.equal(c.send(false), false);
  assert.equal(c.root.dialog.shown, 1);
  assert.equal(cap.seen.length, 0);
  c.confirmSend();
  assert.equal(c.root.dialog.closed, 1);
  assert.equal(cap.seen[0].type, "sstv_tx_start");
  assert.equal(cap.seen[0].transmission_id, "abc");
  assert.equal(cap.seen[0].confirm_no_callsign, true);
  cap.stop();
  c.destroy();
});

await ok("server no_callsign refusal opens the confirmation", () => {
  const c = panel();
  c.preview = { id: "abc", url: "/p", duration_s: 1, mode: "pd120", slot: 1, module: "fm0", noCallsign: false };
  const cap = capture("control:send");
  c.send(false);
  assert.equal(cap.seen[0].confirm_no_callsign, undefined);
  window.dispatchEvent(new CustomEvent("control:error", { detail: { type: "error", request_id: cap.seen[0].request_id, error: { code: "no_callsign" } } }));
  assert.equal(c.root.dialog.shown, 1);
  cap.stop();
  c.destroy();
});

await ok("sending on the ISS downlink is allowed (no guard, user 2026-11-25)", () => {
  const c = panel();
  c.preview = { id: "abc", url: "/p", duration_s: 1, mode: "pd120", slot: 1, module: "fm0", noCallsign: false };
  c._onModuleState({ slot: 1, module: "fm0", values: { tx_frequency: 145.8 } });
  assert.equal(c.txMHz(), "145.8000");
  assert.equal(c.canSend, true);
  assert.equal(c.sendHint(), "");
  const cap = capture("control:send");
  assert.equal(c.send(false), true);
  assert.equal(cap.seen[0].type, "sstv_tx_start");
  cap.stop();
  c.destroy();
});

await ok("refusals at send time show their text", () => {
  const c = panel();
  c.preview = { id: "abc", url: "/p", duration_s: 1, mode: "pd120", slot: 1, module: "fm0", noCallsign: false };
  const cap = capture("control:send");
  c.send(false);
  window.dispatchEvent(new CustomEvent("control:error", { detail: { type: "error", request_id: cap.seen[0].request_id, error: { code: "expired" } } }));
  assert.equal(c.pending, false);
  assert.equal(c.preview, null);
  assert.match(c.notice.text, /older than one hour/);
  cap.stop();
  c.destroy();
});

await ok("transmission: progress, abort, sstv:tx on/off, gallery on done", () => {
  const c = panel();
  c.preview = { id: "t1", url: "/p", duration_s: 127, mode: "pd120", slot: 1, module: "fm0", noCallsign: false };
  const tx = capture("sstv:tx");
  c._onTxState({ type: "sstv_tx_state", id: "t1", state: "sending", progress: 0, duration_s: 127, mode: "pd120" });
  assert.equal(c.txLive, true);
  assert.deepEqual(tx.seen, [{ active: true }]);
  assert.equal(c.canSend, false);
  assert.equal(c.canStartRx, false);
  c._onTxState({ id: "t1", state: "keyed", progress: 0.5, duration_s: 127 });
  assert.equal(c.txPercent(), 50);
  assert.equal(c.summary(), "On the air, 50 %");
  assert.equal(tx.seen.length, 1, "no repeat while it stays on the air");
  const send = capture("control:send");
  c.abort();
  assert.equal(send.seen[0].type, "sstv_tx_stop");
  send.stop();
  c._onTxState({ id: "t1", state: "done", progress: 1, duration_s: 127 });
  assert.deepEqual(tx.seen, [{ active: true }, { active: false }]);
  assert.equal(c.txLive, false);
  assert.equal(c.preview, null);
  assert.equal(c.notice.ok, true);
  tx.stop();
  c.destroy();
});

await ok("a transmission seen by a viewer still blocks and ends cleanly", () => {
  const c = panel({ "data-can-use": "0" });
  const tx = capture("sstv:tx");
  c._onTxState({ id: "t9", state: "keyed", progress: 0.1, duration_s: 60 });
  c._onTxState({ id: "t9", state: "aborted", detail: "lock released" });
  assert.deepEqual(tx.seen, [{ active: true }, { active: false }]);
  assert.equal(c.canAbort, false);
  tx.stop();
  c.destroy();
});

await ok("a page that missed the end frame lets go of a stale transmission", () => {
  const c = panel();
  c._onTxState({ id: "t2", state: "keyed", progress: 0, duration_s: 10 });
  c.tx.startedAt = Date.now() - 10 * 60 * 1000;
  c.now = Date.now();
  c._expireStale();
  assert.equal(c.tx, null);
  c.destroy();
});

await ok("reconnect drops sessions the page may have missed the end of", () => {
  const c = panel();
  const tx = capture("sstv:tx");
  c._onRxState({ state: "running", slot: 1, ends_at: new Date(Date.now() + 600000).toISOString() });
  c._onTxState({ id: "t3", state: "keyed", progress: 0.2, duration_s: 60 });
  window.dispatchEvent(new CustomEvent("control:open", { detail: { type: "open" } }));
  assert.equal(c.rx, null);
  assert.equal(c.tx, null);
  assert.deepEqual(tx.seen, [{ active: true }, { active: false }]);
  assert.equal(c.notice, null, "silent: the snapshot restores what is still live");
  // the snapshot brings a live transmission back
  c._onTxState({ id: "t3", state: "sending", progress: 0, duration_s: 60 });
  assert.equal(c.txLive, true);
  tx.stop();
  c.destroy();
});

await ok("an aborted or failed transmission uses up the preview", () => {
  for (const state of ["aborted", "error"]) {
    const c = panel();
    c.preview = { id: "t4", url: "/p", duration_s: 1, mode: "pd120", slot: 1, module: "fm0", noCallsign: false };
    c._onTxState({ id: "t4", state: "keyed", progress: 0.1, duration_s: 60 });
    c._onTxState({ id: "t4", state, detail: state === "aborted" ? "operator" : "player_failed" });
    assert.equal(c.preview, null, state);
    assert.equal(c.canSend, false, state);
    assert.match(c.notice.text, state === "aborted" ? /^You aborted/ : /failed/);
    c.destroy();
  }
});

await ok("viewers get a neutral text for an operator abort, none for done", () => {
  const c = panel({ "data-can-use": "0" });
  c._onTxState({ id: "t5", state: "keyed", progress: 0.1, duration_s: 60 });
  c._onTxState({ id: "t5", state: "aborted", detail: "operator" });
  assert.equal(c.notice.text, "The transmission was aborted.");
  c.notice = null;
  c._onTxState({ id: "t6", state: "keyed", progress: 0.1, duration_s: 60 });
  c._onTxState({ id: "t6", state: "done" });
  assert.equal(c.notice, null);
  c.destroy();
});

await ok("a preview reply that arrives after the choices changed is dropped", async () => {
  const c = panel();
  let resolve;
  globalThis.fetch = () => new Promise((r) => (resolve = r));
  const pending = c.makePreview();
  c.mode = "martin1";
  c.invalidate();
  resolve({ ok: true, status: 200, json: () => Promise.resolve({ id: "old", preview_url: "/p", duration_s: 1, mode: "pd120", slot: 1, module: "fm0", warnings: [] }) });
  assert.equal(await pending, false);
  assert.equal(c.preview, null);
  assert.equal(c.previewBusy, false);
  c.destroy();
});

await ok("uploads over the size limit never leave the browser", async () => {
  const c = panel({ "data-max-upload-bytes": "100" });
  c.source = "upload";
  c.fileName = "big.jpg";
  const big = { name: "big.jpg", type: "image/jpeg", size: 101 };
  c.root.querySelector = ((orig) => (sel) => (sel === "[data-sv=file]" ? { files: [big] } : orig(sel)))(c.root.querySelector);
  const calls = fakeFetch(200, {});
  assert.equal(await c.makePreview(), false);
  assert.equal(calls.length, 0);
  assert.match(c.notice.text, /larger than/);
  c.destroy();
});

await ok("a sent picture does not replace the monitor's last received picture", () => {
  const c = panel();
  c._onImage({ id: "a", url: "/rx.png", direction: "rx", complete: true });
  c._onImage({ id: "b", url: "/tx.png", direction: "tx", complete: true });
  assert.equal(c.lastImage.url, "/rx.png");
  c.destroy();
});

await ok("a late PNG load never paints over a newer live picture", () => {
  const images = [];
  globalThis.Image = class {
    constructor() {
      images.push(this);
      this.naturalWidth = 320;
      this.naturalHeight = 256;
    }
  };
  const c = panel();
  c._onImage({ id: "a", url: "/rx.png", direction: "rx", complete: true });
  c._onPictureStart({ picture_id: "p9", mode: "pd90", width: 320, height: 256 });
  images[0].onload();
  assert.equal(c.root.canvasCalls.includes("draw"), false);
  // without a newer picture the final PNG is drawn
  c.picture = null;
  c._onImage({ id: "b", url: "/rx2.png", direction: "rx", complete: true });
  images[1].onload();
  assert.equal(c.root.canvasCalls.includes("draw"), true);
  delete globalThis.Image;
  c.destroy();
});

await ok("an inventory without a frequency keeps the last known one", () => {
  const c = panel();
  c._onModuleState({ slot: 1, module: "fm0", values: { tx_frequency: 145.8 } });
  c._seed([{ slot: "1", modules: [{ module: "fm0", online: true, capabilities: FM, state: {} }] }]);
  assert.equal(c.txMHz(), "145.8000");
  c.destroy();
});

await ok("with a preview the TX module is exactly the preview's", () => {
  const c = panel();
  c.preview = { id: "x", url: "/p", duration_s: 1, mode: "pd120", slot: 9, module: "gone", noCallsign: false };
  assert.equal(c.txModule, null);
  assert.equal(c.txMHz(), "–");
  c.preview.slot = 1;
  c.preview.module = "fm0";
  assert.equal(c.txModule.module, "fm0");
  c.destroy();
});

await ok("a live RX on another slot moves the module choice (ISS tunes the receiver)", () => {
  const c = panel();
  assert.equal(c.selected, "1 fm0");
  c._onRxState({ state: "running", slot: 2, ends_at: null });
  assert.equal(c.selected, "2 rx1");
  commands.length = 0;
  c.tuneIss();
  assert.equal(commands[0].slot, 2);
  assert.equal(commands[0].module, "rx1");
  assert.equal(c.rxMHz(), "433.5000");
  c.destroy();
});

await ok("a reconnect mid-picture keeps painting the live picture", () => {
  const c = panel();
  c._onRxState({ state: "running", slot: 1, ends_at: null });
  c._onPictureStart({ picture_id: "p5", mode: "robot36", width: 320, height: 240 });
  window.dispatchEvent(new CustomEvent("control:open", { detail: { type: "open" } }));
  c._onRxState({ state: "running", slot: 1, ends_at: null }); // the snapshot
  assert.equal(c.picture && c.picture.id, "p5");
  const rgb = Buffer.alloc(320 * 3, 9).toString("base64");
  c._onLine({ picture_id: "p5", row: 3, rgb });
  assert.equal(c.picture.row, 3);
  // a fresh start (not a reconnect) still clears an old picture
  c._onRxState({ state: "idle", detail: "operator" });
  c._onRxState({ state: "starting", slot: 1, ends_at: null });
  assert.equal(c.picture, null);
  c.destroy();
});

await ok("an RX state frame does not swallow the refusal of a pending send", () => {
  const c = panel();
  c.preview = { id: "abc", url: "/p", duration_s: 1, mode: "pd120", slot: 1, module: "fm0", noCallsign: false };
  const cap = capture("control:send");
  c.send(false);
  c._onRxState({ state: "running", slot: 1, ends_at: null }); // unrelated broadcast
  assert.equal(c.pending, true, "the send is still open");
  window.dispatchEvent(new CustomEvent("control:error", { detail: { type: "error", request_id: cap.seen[0].request_id, error: { code: "scan_active" } } }));
  assert.equal(c.pending, false);
  assert.match(c.notice.text, /scanner/);
  cap.stop();
  c.destroy();
});

await ok("no answer at all: every open request gives up with one notice", () => {
  const timers = [];
  const realSet = globalThis.setTimeout;
  globalThis.setTimeout = (fn) => (timers.push(fn), 0);
  const c = panel();
  c.startRx();
  c.preview = { id: "abc", url: "/p", duration_s: 1, mode: "pd120", slot: 1, module: "fm0", noCallsign: false };
  globalThis.setTimeout = realSet;
  assert.equal(c.pending, true);
  timers[timers.length - 1]();
  assert.equal(c.pending, false);
  assert.deepEqual(c._requests, {});
  assert.match(c.notice.text, /did not answer/);
  c.destroy();
});

function withTimers(fn) {
  const timers = [];
  const realSet = globalThis.setTimeout;
  globalThis.setTimeout = (cb, ms) => (timers.push({ cb, ms }), timers.length);
  try {
    return fn(timers);
  } finally {
    globalThis.setTimeout = realSet;
  }
}

await ok("idle reconnect: no snapshot within the window drops the partial picture", () => {
  withTimers((timers) => {
    const c = panel();
    c._onRxState({ state: "running", slot: 1, ends_at: null });
    c._onPictureStart({ picture_id: "p6", mode: "robot36", width: 320, height: 240 });
    window.dispatchEvent(new CustomEvent("control:open", { detail: { type: "open" } }));
    assert.equal(c.picture.id, "p6", "kept until the window closes");
    timers.find((t) => t.ms === 5000).cb(); // no live sstv_state came
    assert.equal(c.picture, null);
    assert.equal(c._reopened, false);
    c._onRxState({ state: "starting", slot: 1, ends_at: null });
    assert.equal(c.picture, null);
    c.destroy();
  });
});

await ok("an inventory gap during RX does not move the module choice off the receiver", () => {
  const c = panel();
  c._onRxState({ state: "running", slot: 2, ends_at: null });
  c._seed([INV[0]]); // slot 2 briefly missing
  c._seed(INV);
  assert.equal(c.selected, "2 rx1");
  c.destroy();
});

await ok("a lost ISS reply gives up with a notice; a reconnect drops it silently", () => {
  withTimers((timers) => {
    const c = panel();
    c.tuneIss();
    const giveUp = timers[timers.length - 1];
    assert.equal(Object.values(c._requests).filter((k) => k === "command").length, 1);
    giveUp.cb();
    assert.deepEqual(c._requests, {});
    assert.match(c.notice.text, /ISS frequency could not be set/);
    c.notice = null;
    c.tuneIss();
    const second = timers[timers.length - 1];
    window.dispatchEvent(new CustomEvent("control:open", { detail: { type: "open" } }));
    assert.deepEqual(c._requests, {});
    second.cb();
    assert.equal(c.notice, null);
    c.destroy();
  });
});

await ok("an abandoned picture is wiped, the last stored one comes back, the gallery refreshes", () => {
  const images = [];
  globalThis.Image = class {
    constructor() {
      images.push(this);
      this.naturalWidth = 320;
      this.naturalHeight = 256;
    }
  };
  withTimers((timers) => {
    const c = panel();
    c._onImage({ id: "a", url: "/a.png", direction: "rx", complete: true });
    c._onRxState({ state: "running", slot: 1, ends_at: null });
    c._onPictureStart({ picture_id: "p7", mode: "robot36", width: 320, height: 240 });
    c.root.canvasCalls.length = 0;
    bodyEvents.length = 0;
    window.dispatchEvent(new CustomEvent("control:open", { detail: { type: "open" } }));
    timers.find((t) => t.ms === 5000).cb();
    assert.equal(c.picture, null);
    assert.equal(c.root.canvasCalls[0], "clear");
    assert.equal(images[images.length - 1].src, "/a.png");
    images[images.length - 1].onload();
    assert.ok(c.root.canvasCalls.includes("draw"));
    assert.deepEqual(bodyEvents, ["sstv-gallery-refresh"]);
    c.destroy();
  });
  delete globalThis.Image;
});

await ok("destroy and replies clear the ISS give-up timers", () => {
  const cleared = [];
  const realClear = globalThis.clearTimeout;
  globalThis.clearTimeout = (id) => (cleared.push(id), realClear(id));
  try {
    withTimers(() => {
      const c = panel();
      c.tuneIss();
      const rid = Object.keys(c._requests)[0];
      const timer = c._cmdTimers[rid];
      c._onReply({ type: "result", request_id: rid, ok: true });
      assert.ok(cleared.includes(timer));
      assert.deepEqual(c._cmdTimers, {});
      c.tuneIss();
      const t2 = Object.values(c._cmdTimers)[0];
      c.destroy();
      assert.ok(cleared.includes(t2));
      assert.deepEqual(c._cmdTimers, {});
    });
  } finally {
    globalThis.clearTimeout = realClear;
  }
});

await ok("a kept picture that finished during the gap is dropped when no row follows", () => {
  withTimers((timers) => {
    const c = panel();
    c._onRxState({ state: "running", slot: 1, ends_at: null });
    c._onPictureStart({ picture_id: "p8", mode: "robot36", width: 320, height: 240 });
    window.dispatchEvent(new CustomEvent("control:open", { detail: { type: "open" } }));
    c._onRxState({ state: "running", slot: 1, ends_at: null }); // live snapshot
    assert.equal(c.picture.id, "p8");
    bodyEvents.length = 0;
    timers[timers.length - 1].cb(); // no sstv_line within the window
    assert.equal(c.picture, null);
    assert.deepEqual(bodyEvents, ["sstv-gallery-refresh"]);
    // ... but a picture that keeps receiving rows stays
    c._onPictureStart({ picture_id: "p9", mode: "robot36", width: 320, height: 240 });
    window.dispatchEvent(new CustomEvent("control:open", { detail: { type: "open" } }));
    c._onRxState({ state: "running", slot: 1, ends_at: null });
    c._onLine({ picture_id: "p9", row: 1, rgb: Buffer.alloc(320 * 3).toString("base64") });
    timers[timers.length - 1].cb();
    assert.equal(c.picture.id, "p9");
    c.destroy();
  });
});

await ok("RX readout uses the selected module when it sits on the receiving slot", () => {
  const c = panel();
  c._seed([
    { slot: "1", modules: [
      { module: "a", online: true, capabilities: [{ name: "rx_frequency" }], state: { rx_frequency: 145.5 } },
      { module: "b", online: true, capabilities: [{ name: "rx_frequency" }], state: { rx_frequency: 145.8 } },
    ] },
  ]);
  c.selected = "1 a";
  c._onRxState({ state: "running", slot: 1, ends_at: null });
  assert.equal(c.selected, "1 a");
  assert.equal(c.rxMHz(), "145.5000");
  assert.equal(c.onIss, false);
  c.destroy();
});

// -- control panel: bridge, emit, PTT block ----------------------------------------
await ok("control panel bridges the SSTV frames and re-emits the server's", () => {
  const cp = registered.controlPanel();
  const sent = [];
  cp._send = (f) => (sent.push(f), true);
  cp._bridgeReqs = {};
  for (const t of ["sstv_rx_start", "sstv_rx_stop", "sstv_tx_start", "sstv_tx_stop"]) {
    cp._onSendEvent({ detail: { type: t, request_id: t } });
  }
  assert.deepEqual(sent.map((f) => f.type), ["sstv_rx_start", "sstv_rx_stop", "sstv_tx_start", "sstv_tx_stop"]);
  for (const t of ["sstv_state", "sstv_start", "sstv_line", "sstv_image", "sstv_tx_state"]) {
    const cap = capture("control:" + t);
    cp._route({ type: t });
    cap.stop();
    assert.equal(cap.seen.length, 1, t);
  }
});

await ok("voice PTT is blocked and unkeyed while SSTV is on the air", () => {
  const cp = registered.controlPanel();
  cp.$el = { getAttribute: () => null };
  const sent = [];
  cp._ws = { readyState: 1, send: (s) => sent.push(JSON.parse(s)) };
  globalThis.WebSocket = { OPEN: 1 };
  cp._connect = () => {};
  cp._seedFromInitial = () => {};
  globalThis.localStorage = { getItem: () => null };
  document.removeEventListener = () => {};
  cp.init();
  cp.youHold = true;
  cp.lockState = "held";
  cp.conn = "open";
  cp._ingestInventory([{ slot: "1", modules: [{ module: "fm0", online: true, capabilities: [{ name: "ptt", kind: "action", type: "bool" }] }] }]);
  assert.equal(cp.canKey("1", "fm0"), cp.canOperate("1", "fm0"));
  cp.pttDown("1", "fm0");
  assert.equal(cp.pttState("1", "fm0"), true);
  window.dispatchEvent(new CustomEvent("sstv:tx", { detail: { active: true } }));
  assert.equal(cp.sstvTx, true);
  assert.equal(cp.pttState("1", "fm0"), false, "keyed voice PTT is released");
  assert.equal(cp.canKey("1", "fm0"), false);
  cp.pttDown("1", "fm0");
  assert.equal(cp.pttState("1", "fm0"), false);
  window.dispatchEvent(new CustomEvent("sstv:tx", { detail: { active: false } }));
  assert.equal(cp.canKey("1", "fm0"), cp.canOperate("1", "fm0"));
  cp._teardown();
});

console.log(`# ${passed} passed`);
