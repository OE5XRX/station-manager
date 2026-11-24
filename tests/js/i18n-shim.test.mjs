// i18n shim: when Django's JavaScriptCatalog globals (gettext / interpolate) exist,
// the marked JS files must route their user-visible strings through them; whole
// sentences with named placeholders, never concatenated fragments.
// Run: node tests/js/i18n-shim.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));

// Fake catalog: marks every msgid so we can see it went through gettext, and
// uses Django's interpolate semantics (named %(x)s).
const seen = [];
globalThis.gettext = (s) => {
  seen.push(s);
  return "«" + s + "»";
};
globalThis.interpolate = (fmt, obj, named) => {
  assert.equal(named, true, "only named placeholders are used");
  return fmt.replace(/%\(\s*([^)]+)\s*\)s/g, (m, k) => String(obj[k]));
};

globalThis.window = new EventTarget();
globalThis.document = { addEventListener: () => {}, getElementById: () => null };

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

const L = require(path.resolve(here, "../../static/js/control-logic.js"));
window.OE5XRXControlLogic = L;
const R = require(path.resolve(here, "../../static/js/repeater-panel.js")).logic;
const S = require(path.resolve(here, "../../static/js/scanner-panel.js")).logic;

ok("control-logic error messages go through gettext", () => {
  assert.equal(L.errorMessage("timeout"), "«No response»");
  // Unknown codes are protocol data and stay raw.
  assert.equal(L.errorMessage("weird_code"), "weird_code");
});

ok("passive badge: whole sentence with a named placeholder", () => {
  assert.equal(L.passiveBadge({ state: "error", reason: "no_rx" }).label, "«Passive: error: no_rx»");
  assert.equal(L.passiveBadge({ state: "off", reason: "idle_wait" }).detail, "«Waiting for idle»");
  assert.ok(seen.includes("Passive: error: %(reason)s"));
});

ok("repeater panel: result text is one interpolated sentence", () => {
  assert.equal(R.resultText({ ok: true }, "OE5XRR").text, "«Tuned to OE5XRR.»");
  assert.equal(R.errorText("ptt_active"), "«Release PTT before switching repeaters.»");
});

ok("scanner panel: reasons and errors go through gettext", () => {
  assert.equal(S.reasonText("operator"), "«You stopped the scan.»");
  assert.equal(S.errorText("no_active_scan"), "«No scan is running.»");
});

console.log("\n" + passed + " assertions passed");
