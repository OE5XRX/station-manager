// Pure-logic unit tests for static/js/metrics-history-logic.js.
// Run: TZ=UTC node tests/js/metrics-history-logic.test.mjs  (exit 0 = pass).
// Invoked from pytest via tests/test_metrics_history_js.py (sets TZ=UTC).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const H = require(path.resolve(here, "../../static/js/metrics-history-logic.js"));

let passed = 0;
function ok(name, fn) { fn(); passed += 1; console.log("ok - " + name); }

const NOW = Date.UTC(2026, 10, 5, 12); // 2026-11-05T12:00:00Z

// --- ranges -----------------------------------------------------------------
ok("RANGES match the plan contract", () => {
  assert.deepEqual(H.RANGES, { "1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000, "1y": 31536000 });
  assert.equal(H.DEFAULT_RANGE, "24h");
});

// --- buildUrl ---------------------------------------------------------------
ok("buildUrl 24h window, literal comma key list, auto resolution", () => {
  const url = H.buildUrl(5, ["a.b", "c"], "24h", NOW);
  assert.ok(url.startsWith("/api/v1/stations/5/metrics/history/?"), url);
  const q = new URLSearchParams(url.split("?")[1]);
  assert.ok(url.includes("keys=a.b,c"), url);
  assert.equal(q.get("since"), "2026-11-04T12:00:00.000Z");
  assert.equal(q.get("until"), "2026-11-05T12:00:00.000Z");
  assert.equal(q.get("resolution"), "auto");
});
ok("buildUrl honours every range", () => {
  for (const [key, secs] of Object.entries(H.RANGES)) {
    const q = new URLSearchParams(H.buildUrl(1, ["x"], key, NOW).split("?")[1]);
    assert.equal(Date.parse(q.get("until")) - Date.parse(q.get("since")), secs * 1000, key);
  }
});
ok("buildUrl falls back to 24h for an unknown range", () => {
  const q = new URLSearchParams(H.buildUrl(1, ["x"], "bogus", NOW).split("?")[1]);
  assert.equal(q.get("since"), "2026-11-04T12:00:00.000Z");
});
ok("buildUrl encodes odd key characters but keeps the comma separator", () => {
  const url = H.buildUrl(1, ["a&b", "c"], "1h", NOW);
  assert.ok(url.includes("keys=a%26b,c"), url);
});

// --- parseKeys --------------------------------------------------------------
ok("parseKeys trims and drops empties", () => {
  assert.deepEqual(H.parseKeys(" a.b , ,c,"), ["a.b", "c"]);
  assert.deepEqual(H.parseKeys(""), []);
  assert.deepEqual(H.parseKeys(undefined), []);
});

// --- toUplotData ------------------------------------------------------------
ok("toUplotData([]) -> four empty arrays", () => {
  assert.deepEqual(H.toUplotData([]), [[], [], [], []]);
});
ok("toUplotData single point", () => {
  assert.deepEqual(H.toUplotData([[1, 1, 2, 3]]), [[1], [1], [2], [3]]);
});
ok("toUplotData is null-safe", () => {
  assert.deepEqual(H.toUplotData(null), [[], [], [], []]);
  assert.deepEqual(H.toUplotData(undefined), [[], [], [], []]);
  assert.deepEqual(H.toUplotData("nope"), [[], [], [], []]);
  assert.deepEqual(
    H.toUplotData([[10, null, 2, NaN], null, ["x", 1, 1, 1], [20, 1, "2", 3]]),
    [[10, 20], [null, 1], [2, null], [null, 3]],
  );
});

// --- formatValue ------------------------------------------------------------
ok("formatValue dot decimal at precision", () => {
  assert.equal(H.formatValue(12.345, 2), "12.35");
  assert.equal(H.formatValue(12, 0), "12");
  assert.equal(H.formatValue(1234567.5, 1), "1234567.5"); // no grouping separators
  assert.ok(!H.formatValue(1234.5, 1).includes(","));
});
ok("formatValue em dash for missing values", () => {
  assert.equal(H.formatValue(null, 1), "—");
  assert.equal(H.formatValue(undefined, 1), "—");
  assert.equal(H.formatValue(NaN, 1), "—");
  assert.equal(H.formatValue(Infinity, 1), "—");
  assert.equal(H.formatValue("12", 1), "—");
});
ok("formatValue never shows a signed zero and clamps precision", () => {
  assert.equal(H.formatValue(-0.001, 2), "0.00");
  assert.equal(H.formatValue(-0.5, 0), "-1"); // toFixed rounds half away from zero
  assert.equal(H.formatValue(1.5, undefined), "1.5");
  assert.equal(H.formatValue(1, -3), "1");
  assert.equal(H.formatValue(1, 99), "1.000000");
});

// --- axis -------------------------------------------------------------------
ok("axisDecimals follows the tick increment", () => {
  assert.equal(H.axisDecimals([10, 20, 30], 2), 0);
  assert.equal(H.axisDecimals([11.5, 12, 12.5], 0), 1);
  assert.equal(H.axisDecimals([0.02, 0.04], 1), 2);
  assert.equal(H.axisDecimals([5], 3), 3);
  assert.equal(H.axisDecimals([], 2), 2);
});
ok("axisValues are dot-decimal strings", () => {
  assert.deepEqual(H.axisValues([11.5, 12, 12.5], 2), ["11.5", "12.0", "12.5"]);
});

// --- time -------------------------------------------------------------------
ok("formatTime short and long spans (TZ=UTC)", () => {
  const t = Date.UTC(2026, 10, 5, 9, 7) / 1000;
  assert.equal(H.formatTime(t, 3600), "09:07");
  assert.equal(H.formatTime(t, 86400), "09:07");
  assert.equal(H.formatTime(t, 604800), "2026-11-05 09:07");
  assert.equal(H.formatTime(null, 3600), "—");
});
ok("axisTimeValues per span (TZ=UTC)", () => {
  const t = Date.UTC(2026, 10, 5, 9, 0) / 1000;
  assert.deepEqual(H.axisTimeValues([t, t + 3600], 86400), ["09:00", "10:00"]);
  assert.deepEqual(H.axisTimeValues([t, t + 86400], 604800), ["11-05", "11-06"]);
  assert.deepEqual(H.axisTimeValues([t, t + 43200], 604800), ["11-05 09:00", "11-05 21:00"]);
  assert.deepEqual(H.axisTimeValues([t], 31536000), ["2026-11"]);
  assert.deepEqual(H.axisTimeValues(null, 3600), []);
  assert.deepEqual(H.axisTimeValues([null], 3600), [""]);
});
ok("isoToSeconds", () => {
  assert.equal(H.isoToSeconds("2026-11-05T12:00:00Z"), NOW / 1000);
  assert.equal(H.isoToSeconds("garbage"), null);
  assert.equal(H.isoToSeconds(undefined), null);
});
ok("lastIndex skips trailing gaps", () => {
  assert.equal(H.lastIndex([[1, 2, 3], [1, 1, 1], [5, 6, null], [1, 1, 1]]), 1);
  assert.equal(H.lastIndex([[], [], [], []]), -1);
  assert.equal(H.lastIndex(null), -1);
});

// --- prepareCharts (Review Focus 5: station without history) ---------------
ok("prepareCharts: no history -> empty, one descriptor per requested key", () => {
  const r = H.prepareCharts(
    { since: "2026-11-04T12:00:00Z", until: "2026-11-05T12:00:00Z", resolution: "5m", source: "raw", series: [] },
    ["pwr.12v.voltage_v", "cpu.temp_c"],
  );
  assert.equal(r.empty, true);
  assert.deepEqual(r.charts.map((c) => c.key), ["pwr.12v.voltage_v", "cpu.temp_c"]);
  assert.equal(r.charts[0].label, "pwr.12v.voltage_v");
  assert.deepEqual(r.charts[0].data, [[], [], [], []]);
  assert.equal(r.sinceS, NOW / 1000 - 86400);
  assert.equal(r.resolution, "5m");
});
ok("prepareCharts: series with empty points still counts as empty", () => {
  const r = H.prepareCharts(
    { series: [{ key: "a", label: "A", unit: "V", precision: 2, group: "power", points: [] }] },
    ["a"],
  );
  assert.equal(r.empty, true);
  assert.equal(r.charts[0].label, "A");
  assert.equal(r.charts[0].unit, "V");
  assert.equal(r.charts[0].precision, 2);
});
ok("prepareCharts: malformed responses never throw", () => {
  for (const bad of [null, undefined, "x", 42, [], { series: "nope" }, { series: [null, 1, {}] }]) {
    const r = H.prepareCharts(bad, ["a"]);
    assert.equal(r.empty, true);
    assert.equal(r.charts.length, 1);
    assert.equal(r.sinceS, null);
  }
  assert.deepEqual(H.prepareCharts(null, []).charts, []);
});
ok("prepareCharts: keeps request order and maps data", () => {
  const r = H.prepareCharts(
    {
      resolution: "1h",
      series: [
        { key: "b", label: "B", unit: "", precision: 0, points: [[100, 1, 2, 3], [200, 2, 3, 4]] },
        { key: "a", label: "A", unit: "V", precision: 2, points: [] },
      ],
    },
    ["a", "b"],
  );
  assert.equal(r.empty, false);
  assert.deepEqual(r.charts.map((c) => c.key), ["a", "b"]);
  assert.equal(r.charts[1].count, 2);
  assert.deepEqual(r.charts[1].data, [[100, 200], [1, 2], [2, 3], [3, 4]]);
});

console.log(`# ${passed} passed`);
