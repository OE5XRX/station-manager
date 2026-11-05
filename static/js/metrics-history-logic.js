/* OE5XRX Metric history — pure logic
   No DOM, no fetch, no uPlot. Every function here is deterministic and
   unit-tested from Node (tests/js/metrics-history-logic.test.mjs). The browser
   glue (static/js/metrics-history.js) consumes this via
   window.OE5XRXMetricsHistoryLogic; Node consumes it via require().

   UMD: attaches to module.exports when present, else
   window.OE5XRXMetricsHistoryLogic. Style mirrors audio-logic.js — ES5-ish,
   "use strict", var/function.

   Numbers are always formatted with a dot decimal separator (Number#toFixed),
   never toLocaleString: the UI runs under de_AT where the locale formatter
   would emit a comma and the chart would disagree with the rest of the tab. */

(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.OE5XRXMetricsHistoryLogic = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Selectable time ranges (seconds). Order = button order in the panel.
  var RANGES = {
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
    "30d": 2592000,
    "1y": 31536000,
  };
  var DEFAULT_RANGE = "24h";
  var EMPTY = "—"; // em dash for "no value"
  var MAX_DECIMALS = 6;
  // Server-side cap on keys per history request (apps.history.query.MAX_KEYS).
  // A metric group may hold more, so the glue splits it into chunks.
  var MAX_KEYS_PER_REQUEST = 10;
  // Nominal bucket width per resolution (seconds); raw is derived from data.
  var STEP_SECONDS = { "5m": 300, "1h": 3600, "1d": 86400 };
  var RAW_MIN_STEP = 60;
  var GAP_FACTOR = 2.5;

  function rangeSeconds(rangeKey) {
    return Object.prototype.hasOwnProperty.call(RANGES, rangeKey)
      ? RANGES[rangeKey]
      : RANGES[DEFAULT_RANGE];
  }

  /** Comma-separated data attribute -> list of non-empty, trimmed keys. */
  function parseKeys(raw) {
    if (typeof raw !== "string") return [];
    return raw
      .split(",")
      .map(function (k) { return k.trim(); })
      .filter(function (k) { return k.length > 0; });
  }

  /** History API URL for ``keys`` over the window ending at ``nowMs``.

      Keys are URI-encoded individually and joined with a literal comma
      (metric keys are [a-z0-9._], so the comma is unambiguous). Unknown
      range keys fall back to the 24 h default. */
  function buildUrl(stationId, keys, rangeKey, nowMs) {
    var untilMs = typeof nowMs === "number" && isFinite(nowMs) ? nowMs : Date.now();
    var sinceMs = untilMs - rangeSeconds(rangeKey) * 1000;
    var list = (keys || []).map(function (k) { return encodeURIComponent(String(k)); });
    return (
      "/api/v1/stations/" + encodeURIComponent(String(stationId)) + "/metrics/history/" +
      "?keys=" + list.join(",") +
      "&since=" + encodeURIComponent(new Date(sinceMs).toISOString()) +
      "&until=" + encodeURIComponent(new Date(untilMs).toISOString()) +
      "&resolution=auto"
    );
  }

  function num(v) {
    return typeof v === "number" && isFinite(v) ? v : null;
  }

  /** Split ``keys`` into consecutive chunks of at most ``size``
      (default MAX_KEYS_PER_REQUEST), preserving order. */
  function chunkKeys(keys, size) {
    var list = Array.isArray(keys) ? keys : [];
    var n = typeof size === "number" && size >= 1 ? Math.floor(size) : MAX_KEYS_PER_REQUEST;
    var out = [];
    for (var i = 0; i < list.length; i += n) out.push(list.slice(i, i + n));
    return out;
  }

  /** Merge per-chunk API responses into one: ``series`` concatenated in
      chunk order, response-level fields (since/until/resolution) from the
      first response. Inputs are not mutated. Never throws. */
  function mergeResponses(responses) {
    var list = Array.isArray(responses) ? responses : [];
    var first = list[0] && typeof list[0] === "object" ? list[0] : {};
    var merged = {};
    for (var k in first) {
      if (Object.prototype.hasOwnProperty.call(first, k)) merged[k] = first[k];
    }
    merged.series = [];
    for (var i = 0; i < list.length; i++) {
      var r = list[i];
      if (r && Array.isArray(r.series)) merged.series = merged.series.concat(r.series);
    }
    return merged;
  }

  /** Expected spacing (seconds) for ``resolution``: fixed for 5m/1h/1d;
      for raw max(60, median spacing of ``points``). null = unknown. */
  function stepSeconds(resolution, points) {
    if (Object.prototype.hasOwnProperty.call(STEP_SECONDS, resolution)) {
      return STEP_SECONDS[resolution];
    }
    if (resolution !== "raw") return null;
    var xs = [];
    var list = Array.isArray(points) ? points : [];
    for (var i = 0; i < list.length; i++) {
      if (Array.isArray(list[i]) && num(list[i][0]) !== null) xs.push(list[i][0]);
    }
    var dx = [];
    for (var j = 1; j < xs.length; j++) dx.push(xs[j] - xs[j - 1]);
    if (!dx.length) return RAW_MIN_STEP;
    dx.sort(function (a, b) { return a - b; });
    var mid = Math.floor(dx.length / 2);
    var median = dx.length % 2 ? dx[mid] : (dx[mid - 1] + dx[mid]) / 2;
    return Math.max(RAW_MIN_STEP, median);
  }

  /** API points ``[[ts, min, avg, max], ...]`` -> uPlot columns
      ``[xs, mins, avgs, maxs]``. Malformed points are skipped, non-finite
      values become null (uPlot renders a gap). Never throws.

      With a positive ``step`` (seconds), a null point at ``prev + step`` is
      inserted wherever two points are more than GAP_FACTOR * step apart, so
      offline periods break the avg line and the min/max band (nulls in all
      three series) instead of being bridged. Without ``step`` no gaps are
      inserted. */
  function toUplotData(points, step) {
    var xs = [], mins = [], avgs = [], maxs = [];
    if (!Array.isArray(points)) return [xs, mins, avgs, maxs];
    var gap = typeof step === "number" && isFinite(step) && step > 0 ? step : 0;
    for (var i = 0; i < points.length; i++) {
      var p = points[i];
      if (!Array.isArray(p) || num(p[0]) === null) continue;
      var prev = xs.length ? xs[xs.length - 1] : null;
      if (gap && prev !== null && p[0] - prev > GAP_FACTOR * gap) {
        xs.push(prev + gap);
        mins.push(null);
        avgs.push(null);
        maxs.push(null);
      }
      xs.push(p[0]);
      mins.push(num(p[1]));
      avgs.push(num(p[2]));
      maxs.push(num(p[3]));
    }
    return [xs, mins, avgs, maxs];
  }

  /** Number of real (non-synthetic) points in uPlot columns. */
  function realCount(data) {
    var n = 0;
    for (var i = 0; i < data[0].length; i++) {
      if (data[1][i] !== null || data[2][i] !== null || data[3][i] !== null) n++;
    }
    return n;
  }

  function clampDecimals(precision, fallback) {
    var d = typeof precision === "number" && isFinite(precision) ? Math.round(precision) : fallback;
    if (d < 0) d = 0;
    if (d > MAX_DECIMALS) d = MAX_DECIMALS;
    return d;
  }

  /** Dot-decimal value text; em dash for null/undefined/NaN/non-numbers. */
  function formatValue(v, precision) {
    if (typeof v !== "number" || !isFinite(v)) return EMPTY;
    var s = v.toFixed(clampDecimals(precision, 1));
    // -0.001.toFixed(2) === "-0.00": a signed zero reads like a fault.
    if (/^-0(\.0+)?$/.test(s)) s = s.slice(1);
    return s;
  }

  /** Decimals for y-axis tick labels: enough to tell neighbouring ticks
      apart, never more than MAX_DECIMALS. Falls back to ``precision`` when
      the increment cannot be determined (single tick). */
  function axisDecimals(splits, precision) {
    var fallback = clampDecimals(precision, 1);
    if (!Array.isArray(splits) || splits.length < 2) return fallback;
    var incr = Math.abs(splits[1] - splits[0]);
    if (!isFinite(incr) || incr <= 0) return fallback;
    var d = Math.ceil(-Math.log10(incr) - 1e-9);
    return clampDecimals(d < 0 ? 0 : d, fallback);
  }

  /** Tick labels for the y axis (dot decimal). */
  function axisValues(splits, precision) {
    var d = axisDecimals(splits, precision);
    return (splits || []).map(function (v) { return formatValue(v, d); });
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  /** Local-time label for a point: "HH:MM" for windows up to a day,
      "YYYY-MM-DD HH:MM" for longer ones. ``null`` -> em dash. */
  function formatTime(epochS, spanS) {
    if (typeof epochS !== "number" || !isFinite(epochS)) return EMPTY;
    var d = new Date(epochS * 1000);
    var hm = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    if (typeof spanS === "number" && spanS <= RANGES["24h"]) return hm;
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) + " " + hm;
  }

  /** X-axis tick labels in local time, unambiguous and locale-free:
      "HH:MM" up to a day, "MM-DD" up to 30 days (with "HH:MM" when the
      ticks are sub-daily), "YYYY-MM" beyond. */
  function axisTimeValues(splits, spanS) {
    var list = Array.isArray(splits) ? splits : [];
    var incr = list.length > 1 ? Math.abs(list[1] - list[0]) : 0;
    return list.map(function (t) {
      if (typeof t !== "number" || !isFinite(t)) return "";
      var d = new Date(t * 1000);
      var hm = pad2(d.getHours()) + ":" + pad2(d.getMinutes());
      var md = pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
      if (typeof spanS === "number" && spanS <= RANGES["24h"]) return hm;
      if (typeof spanS === "number" && spanS <= RANGES["30d"]) {
        return incr && incr < 86400 ? md + " " + hm : md;
      }
      return d.getFullYear() + "-" + pad2(d.getMonth() + 1);
    });
  }

  /** ISO 8601 -> epoch seconds, or null when unparsable. */
  function isoToSeconds(iso) {
    if (typeof iso !== "string") return null;
    var ms = Date.parse(iso);
    return isFinite(ms) ? Math.floor(ms / 1000) : null;
  }

  /** Index of the newest point with a non-null avg, or -1. */
  function lastIndex(data) {
    var avgs = data && data[2];
    if (!Array.isArray(avgs)) return -1;
    for (var i = avgs.length - 1; i >= 0; i--) {
      if (avgs[i] !== null) return i;
    }
    return -1;
  }

  /** Normalise an API response into chart descriptors, one per requested key
      in request order. A key missing from the response still yields a
      descriptor (with empty data) so the panel layout stays stable.

      Returns {empty, sinceS, untilS, resolution, charts:[{key, label, unit,
      precision, data, count}]}. ``empty`` is true when no chart has a point —
      a new station or one whose metrics are all hidden — and the panel shows
      the empty state instead of blank axes. Never throws on malformed input. */
  function prepareCharts(response, keys) {
    var body = response && typeof response === "object" ? response : {};
    var byKey = {};
    var list = Array.isArray(body.series) ? body.series : [];
    for (var i = 0; i < list.length; i++) {
      var s = list[i];
      if (s && typeof s.key === "string") byKey[s.key] = s;
    }
    var order = Array.isArray(keys) && keys.length ? keys : Object.keys(byKey);
    var resolution = typeof body.resolution === "string" ? body.resolution : "";
    var charts = [];
    var total = 0;
    for (var j = 0; j < order.length; j++) {
      var key = order[j];
      var src = byKey[key] || {};
      var data = toUplotData(src.points, stepSeconds(resolution, src.points));
      var count = realCount(data);
      total += count;
      charts.push({
        key: key,
        label: typeof src.label === "string" && src.label ? src.label : key,
        unit: typeof src.unit === "string" ? src.unit : "",
        precision: clampDecimals(src.precision, 1),
        data: data,
        count: count,
      });
    }
    return {
      empty: total === 0,
      sinceS: isoToSeconds(body.since),
      untilS: isoToSeconds(body.until),
      resolution: resolution,
      charts: charts,
    };
  }

  return {
    RANGES: RANGES,
    DEFAULT_RANGE: DEFAULT_RANGE,
    MAX_KEYS_PER_REQUEST: MAX_KEYS_PER_REQUEST,
    chunkKeys: chunkKeys,
    mergeResponses: mergeResponses,
    stepSeconds: stepSeconds,
    rangeSeconds: rangeSeconds,
    parseKeys: parseKeys,
    buildUrl: buildUrl,
    toUplotData: toUplotData,
    formatValue: formatValue,
    axisDecimals: axisDecimals,
    axisValues: axisValues,
    formatTime: formatTime,
    axisTimeValues: axisTimeValues,
    isoToSeconds: isoToSeconds,
    lastIndex: lastIndex,
    prepareCharts: prepareCharts,
  };
});
