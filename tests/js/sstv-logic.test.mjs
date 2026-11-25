// SSTV section: pure helpers (static/js/sstv-logic.js).
// Run: node tests/js/sstv-logic.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const S = require(path.resolve(here, "../../static/js/sstv-logic.js"));

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

ok("decodeRow turns base64 RGB into opaque RGBA", () => {
  const b64 = Buffer.from([255, 0, 0, 0, 128, 255, 1, 2, 3]).toString("base64");
  const px = S.decodeRow(b64);
  assert.ok(px instanceof Uint8ClampedArray);
  assert.deepEqual(Array.from(px), [255, 0, 0, 255, 0, 128, 255, 255, 1, 2, 3, 255]);
});

ok("decodeRow drops a partial pixel and survives garbage", () => {
  const b64 = Buffer.from([9, 9, 9, 7, 7]).toString("base64");
  assert.deepEqual(Array.from(S.decodeRow(b64)), [9, 9, 9, 255]);
  assert.equal(S.decodeRow("***not base64***").length, 0);
  assert.equal(S.decodeRow("").length, 0);
  assert.equal(S.decodeRow(null).length, 0);
});

ok("decodeRow handles a full PD 120 row", () => {
  const row = Buffer.alloc(640 * 3, 200);
  assert.equal(S.decodeRow(row.toString("base64")).length, 640 * 4);
});

ok("formatRemaining is mm:ss, never negative, empty without an end", () => {
  const now = Date.parse("2026-11-25T10:00:00Z");
  assert.equal(S.formatRemaining("2026-11-25T10:30:00+00:00", now), "30:00");
  assert.equal(S.formatRemaining("2026-11-25T10:01:05.400Z", now), "01:06");
  assert.equal(S.formatRemaining("2026-11-25T10:00:09Z", now), "00:09");
  assert.equal(S.formatRemaining("2026-11-25T09:59:00Z", now), "00:00");
  assert.equal(S.formatRemaining(null, now), "");
  assert.equal(S.formatRemaining("soon", now), "");
});

ok("isIssFrequency: 145.800 MHz ±10 kHz", () => {
  assert.equal(S.isIssFrequency(145800000), true);
  assert.equal(S.isIssFrequency(145810000), true);
  assert.equal(S.isIssFrequency(145789999), false);
  assert.equal(S.isIssFrequency(145500000), false);
  assert.equal(S.isIssFrequency("145800000"), true);
  assert.equal(S.isIssFrequency(null), false);
  assert.equal(S.isIssFrequency(undefined), false);
  assert.equal(S.isIssFrequency(""), false);
  assert.equal(S.isIssFrequency(NaN), false);
});

ok("mhzToHz / formatMHz", () => {
  assert.equal(S.mhzToHz(145.8), 145800000);
  assert.equal(S.mhzToHz(true), null);
  assert.equal(S.mhzToHz(-1), null);
  assert.equal(S.formatMHz(145800000), "145.8000");
  assert.equal(S.formatMHz(null), "");
});

ok("sendGuardMessage covers every server refusal code", () => {
  const fallback = S.sendGuardMessage("something_new");
  const codes = [
    "not_locked", "forbidden", "unsupported", "invalid_slot", "sstv_busy", "not_found", "expired",
    "no_callsign", "module_offline", "no_frequency", "iss_downlink", "out_of_range", "aborted",
    "scan_active", "ptt_active", "no_active_sstv", "not_connected", "timeout", "bad_mode",
    "bad_source", "no_file", "no_module", "internal_error",
  ];
  const seen = new Set();
  for (const code of codes) {
    const text = S.sendGuardMessage(code);
    assert.ok(text && text !== fallback, code);
    seen.add(text);
  }
  assert.equal(seen.size, codes.length, "every code has its own text");
  assert.match(S.sendGuardMessage("iss_downlink"), /145\.800/);
  assert.ok(fallback);
});

ok("radioModules: online modules of numeric slots with RX/TX frequency", () => {
  const slots = [
    { slot: "1", modules: [{ module: "fm0", online: true, capabilities: [{ name: "rx_frequency" }, { name: "tx_frequency" }] }] },
    { slot: "2", modules: [{ module: "fm1", online: false, capabilities: [{ name: "rx_frequency" }] }] },
    { slot: "3", modules: [{ module: "pb", online: true, capabilities: [{ name: "ptt" }] }] },
    { slot: "audio", modules: [{ module: "router", online: true, capabilities: [{ name: "rx_frequency" }] }] },
    { slot: 4, modules: [{ module: "rx", online: true, capabilities: [{ name: "rx_frequency" }, null] }] },
  ];
  assert.deepEqual(S.radioModules(slots), [
    { slot: "1", module: "fm0", rx: true, tx: true },
    { slot: "4", module: "rx", rx: true, tx: false },
  ]);
  assert.deepEqual(S.radioModules(null), []);
});

ok("end texts: operator stop is silent, the rest explains", () => {
  assert.equal(S.rxEndText("idle", "operator"), "");
  assert.equal(S.rxEndText("idle", null), "");
  assert.match(S.rxEndText("idle", "max_runtime"), /30 minutes/);
  assert.ok(S.rxEndText("idle", "lock released"));
  assert.ok(S.rxEndText("error", "unit_failed"));
  assert.ok(S.rxEndText("idle", "scan started"));
  assert.ok(S.txEndText("done"));
  assert.ok(S.txEndText("aborted", "operator"));
  assert.equal(S.txEndText("aborted", "iss_downlink"), S.sendGuardMessage("iss_downlink"));
  assert.ok(S.txEndText("error", "player_failed rc=1"));
  assert.ok(S.txPhaseText("keyed"));
  assert.equal(S.txPhaseText("done"), "");
});

console.log(`# ${passed} passed`);
