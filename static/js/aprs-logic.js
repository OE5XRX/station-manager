/* OE5XRX APRS page — pure logic
   No DOM, no fetch, no Leaflet. Deterministic and unit-tested from Node
   (tests/js/aprs-logic.test.mjs); static/js/aprs-map.js consumes it via
   window.OE5XRXAprsLogic. UMD like metrics-history-logic.js. */

(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.OE5XRXAprsLogic = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var WINDOW_MS = 30000;

  // TNC2 "SRC>DST,PATH:payload" without the path: "SRC>DST:payload".
  function pathlessKey(raw) {
    var s = String(raw || "");
    var colon = s.indexOf(":");
    if (colon < 0) return s;
    var head = s.slice(0, colon);
    var comma = head.indexOf(",");
    if (comma >= 0) head = head.slice(0, comma);
    return head + s.slice(colon);
  }

  function ts(p) {
    return Date.parse(p.heard_at);
  }

  // Merge one packet heard by several stations within 30 s. Input order does
  // not matter; output is newest first, each group anchored at its newest packet.
  function groupDuplicates(packets) {
    var sorted = (packets || []).slice().sort(function (a, b) {
      return ts(b) - ts(a);
    });
    var groups = [];
    var open = {};
    sorted.forEach(function (p) {
      var key = pathlessKey(p.raw_tnc2);
      var g = open[key];
      if (g && g.oldest - ts(p) <= WINDOW_MS) {
        if (g.stations.indexOf(p.station_name) < 0) g.stations.push(p.station_name);
        g.oldest = ts(p);
        return;
      }
      g = { key: key, packet: p, stations: [p.station_name], oldest: ts(p) };
      open[key] = g;
      groups.push(g);
    });
    return groups.map(function (g) {
      return { key: g.key, packet: g.packet, stations: g.stations };
    });
  }

  function hasPosition(p) {
    return p.latitude !== null && p.latitude !== undefined &&
      p.longitude !== null && p.longitude !== undefined;
  }

  // Newest position per source callsign (packets without coordinates are ignored).
  function latestPositions(packets) {
    var map = new Map();
    (packets || []).forEach(function (p) {
      if (!hasPosition(p)) return;
      var cur = map.get(p.source);
      if (!cur || ts(p) > ts(cur)) map.set(p.source, p);
    });
    return map;
  }

  function formatAge(seconds) {
    var s = Math.max(0, Math.floor(Number(seconds) || 0));
    if (s < 60) return s + " s";
    if (s < 3600) return Math.floor(s / 60) + " min";
    if (s < 86400) return Math.floor(s / 3600) + " h";
    return Math.floor(s / 86400) + " d";
  }

  return {
    groupDuplicates: groupDuplicates,
    latestPositions: latestPositions,
    formatAge: formatAge,
    pathlessKey: pathlessKey,
  };
});
