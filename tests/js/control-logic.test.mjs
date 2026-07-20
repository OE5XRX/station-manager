// Pure-logic unit tests for static/js/control-logic.js.
// Run: node tests/js/control-logic.test.mjs  (exit 0 = pass).
// Invoked from pytest via tests/test_control_logic_js.py.

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const L = require(path.resolve(here, "../../static/js/control-logic.js"));

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

// --- DE-locale parse --------------------------------------------------------
ok("parseNumber comma decimal", () => {
  assert.equal(L.parseNumber("145,5"), 145.5);
});
ok("parseNumber dot decimal", () => {
  assert.equal(L.parseNumber("145.5"), 145.5);
});
ok("parseNumber integer", () => {
  assert.equal(L.parseNumber("146"), 146);
});
ok("parseNumber rejects non-numeric", () => {
  assert.equal(L.parseNumber("abc"), null);
});
ok("parseNumber rejects trailing garbage", () => {
  assert.equal(L.parseNumber("1.2x"), null);
});
ok("parseNumber rejects empty", () => {
  assert.equal(L.parseNumber(""), null);
  assert.equal(L.parseNumber(null), null);
  assert.equal(L.parseNumber(undefined), null);
});
ok("parseNumber passes through finite number", () => {
  assert.equal(L.parseNumber(145.5), 145.5);
  assert.equal(L.parseNumber(NaN), null);
});

// --- serialize dot-decimal --------------------------------------------------
ok("formatNumber integer float renders no fraction", () => {
  assert.equal(L.formatNumber(146.0), "146");
});
ok("formatNumber keeps decimal, dot only", () => {
  const s = L.formatNumber(145.5);
  assert.equal(s, "145.5");
  assert.equal(s.indexOf(","), -1);
  assert.equal(s.toLowerCase().indexOf("e"), -1);
});
ok("formatNumber empty for non-finite", () => {
  assert.equal(L.formatNumber(NaN), "");
  assert.equal(L.formatNumber(null), "");
});

// --- PTT keyboard guards ----------------------------------------------------
const body = { tagName: "BODY", isContentEditable: false };
const input = { tagName: "INPUT", isContentEditable: false };

ok("shouldIgnoreKey ignores auto-repeat", () => {
  assert.equal(L.shouldIgnoreKey({ repeat: true, key: " " }, body, " "), true);
});
ok("shouldIgnoreKey ignores typing in input", () => {
  assert.equal(L.shouldIgnoreKey({ repeat: false, key: " " }, input, " "), true);
});
ok("shouldIgnoreKey ignores contenteditable", () => {
  const ce = { tagName: "DIV", isContentEditable: true };
  assert.equal(L.shouldIgnoreKey({ repeat: false, key: " " }, ce, " "), true);
});
ok("shouldIgnoreKey allows correct key not-typing not-repeat", () => {
  assert.equal(L.shouldIgnoreKey({ repeat: false, key: " " }, body, " "), false);
});
ok("shouldIgnoreKey ignores wrong key", () => {
  assert.equal(L.shouldIgnoreKey({ repeat: false, key: "a" }, body, " "), true);
});
ok("isPttKey accepts Spacebar alias", () => {
  assert.equal(L.isPttKey({ key: "Spacebar" }, " "), true);
  assert.equal(L.isPttKey({ key: " " }, " "), true);
  assert.equal(L.isPttKey({ key: "x" }, " "), false);
});

// --- PTT state machine ------------------------------------------------------
ok("nextPttPhase armed+down -> keying", () => {
  assert.equal(L.nextPttPhase("armed", "down"), "keying");
});
ok("nextPttPhase keying+confirm -> tx", () => {
  assert.equal(L.nextPttPhase("keying", "confirm"), "tx");
});
ok("nextPttPhase tx+release -> armed", () => {
  assert.equal(L.nextPttPhase("tx", "release"), "armed");
});
ok("nextPttPhase keying+release -> armed", () => {
  assert.equal(L.nextPttPhase("keying", "release"), "armed");
});
ok("nextPttPhase armed+confirm stays armed (no spurious tx)", () => {
  assert.equal(L.nextPttPhase("armed", "confirm"), "armed");
});
ok("isKeyed reflects keying/tx", () => {
  assert.equal(L.isKeyed("armed"), false);
  assert.equal(L.isKeyed("keying"), true);
  assert.equal(L.isKeyed("tx"), true);
});

// --- telemetry percent ------------------------------------------------------
ok("telemetryPercent midpoint", () => {
  assert.equal(L.telemetryPercent(50, 0, 100), 50);
});
ok("telemetryPercent clamps low/high", () => {
  assert.equal(L.telemetryPercent(-10, 0, 100), 0);
  assert.equal(L.telemetryPercent(200, 0, 100), 100);
});
ok("telemetryPercent missing range -> 0", () => {
  assert.equal(L.telemetryPercent(50, null, null), 0);
  assert.equal(L.telemetryPercent(50, 5, 5), 0);
});
ok("telemetryPercent parses DE-locale value", () => {
  assert.equal(L.telemetryPercent("2,5", 0, 5), 50);
});

// --- step + clamp -----------------------------------------------------------
ok("computeStep int up by 1", () => {
  assert.equal(L.computeStep(5, 1, null, 0, 10, "int"), 6);
});
ok("computeStep float default 0.1", () => {
  assert.equal(L.computeStep(145.5, 1, null, 144, 146, "float"), 145.6);
});
ok("computeStep clamps to max", () => {
  assert.equal(L.computeStep(146, 1, 1, 144, 146, "float"), 146);
});
ok("computeStep anchors to min when current unparseable", () => {
  assert.equal(L.computeStep("abc", 1, 1, 3, 10, "int"), 3);
  assert.equal(L.computeStep("abc", 1, 1, null, 10, "int"), null);
});

// --- lock derivation --------------------------------------------------------
ok("deriveLockState free", () => {
  assert.equal(L.deriveLockState("free", false), "free");
});
ok("deriveLockState held+you", () => {
  assert.equal(L.deriveLockState("held", true), "held");
});
ok("deriveLockState held+other", () => {
  assert.equal(L.deriveLockState("held", false), "other");
});

// --- error mapping ----------------------------------------------------------
ok("errorMessage maps known codes", () => {
  assert.equal(L.errorMessage("out_of_range"), "Out of range");
  assert.equal(L.errorMessage("timeout"), "No response");
});
ok("errorMessage falls back to raw code", () => {
  assert.equal(L.errorMessage("weird_code"), "weird_code");
  assert.equal(L.errorMessage(""), "");
});

// --- subscribe interval -----------------------------------------------------
ok("subscribeInterval default 500 when no floor", () => {
  assert.equal(L.subscribeInterval([{ name: "rssi" }]), 500);
  assert.equal(L.subscribeInterval([]), 500);
});
ok("subscribeInterval honors slowest floor", () => {
  assert.equal(
    L.subscribeInterval([{ min_interval_ms: 300 }, { min_interval_ms: 1000 }]),
    1000,
  );
});
ok("subscribeInterval clamps below 250", () => {
  assert.equal(L.subscribeInterval([{ min_interval_ms: 100 }]), 250);
});

// --- keys -------------------------------------------------------------------
ok("widgetKey / moduleKey composite", () => {
  assert.equal(L.widgetKey("slot0", "fm0", "frequency"), "slot0 fm0 frequency");
  assert.equal(L.moduleKey("slot0", "fm0"), "slot0 fm0");
});

// --- §7 envelope ------------------------------------------------------------
// Regression: the agent's parse_message drops any frame whose "v" != 1, so
// every browser->server frame MUST be stamped. Missing this = every command /
// subscribe / ptt is silently rejected and the browser only sees "No response".
ok("PROTOCOL_VERSION is 1", () => {
  assert.equal(L.PROTOCOL_VERSION, 1);
});
ok("envelope stamps v:1 on a command frame", () => {
  const f = L.envelope({ type: "command", capability: "frequency", op: "set", value: 145.5 });
  assert.equal(f.v, 1);
  assert.equal(f.type, "command");
  assert.equal(f.capability, "frequency");
  assert.equal(f.value, 145.5);
});
ok("envelope stamps every frame type the agent parses", () => {
  for (const t of ["command", "subscribe", "unsubscribe", "ptt_keepalive"]) {
    assert.equal(L.envelope({ type: t }).v, 1, t + " must carry v:1");
  }
});
ok("envelope returns a copy and does not mutate the input", () => {
  const src = { type: "command" };
  const f = L.envelope(src);
  assert.equal(src.v, undefined); // input untouched
  assert.notEqual(f, src);
});

console.log("\n" + passed + " assertions passed");
