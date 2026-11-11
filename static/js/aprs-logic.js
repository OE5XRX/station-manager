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
  // opts.directOnly: only packets heard without a digipeater count.
  function latestPositions(packets, opts) {
    var directOnly = !!(opts && opts.directOnly);
    var map = new Map();
    (packets || []).forEach(function (p) {
      if (!hasPosition(p)) return;
      if (directOnly && !isDirect(p)) return;
      var cur = map.get(p.source);
      if (!cur || ts(p) > ts(cur)) map.set(p.source, p);
    });
    return map;
  }

  // Generic path aliases are not stations: they never have a position of their own.
  var ALIAS = /^(WIDE|TRACE|RELAY|ECHO|GATE|TCPIP|TCPXX|NOGATE|RFONLY|TEMP|SGATE|CQ|Q[A-Z]{2})\d*(-\d+)?$/i;

  function isAlias(call) {
    return ALIAS.test(String(call || ""));
  }

  // path: ["DB0ABC*", "WIDE2-1"]. The element carrying "*" is the last digipeater that
  // repeated the packet; everything up to and including it was used, the rest is unused.
  function parsePath(path) {
    var hops = [];
    var star = -1;
    (Array.isArray(path) ? path : []).forEach(function (raw, i) {
      var s = String(raw);
      var starred = s.charAt(s.length - 1) === "*";
      var call = starred ? s.slice(0, -1) : s;
      if (starred) star = i;
      hops.push({ call: call, starred: starred, used: false, alias: isAlias(call) });
    });
    hops.forEach(function (h, i) { h.used = i <= star; });
    var usedReal = hops.filter(function (h) { return h.used && !h.alias; });
    return {
      hops: hops,
      direct: star < 0,
      // Station named in "über <DIGI>": the last real digipeater, else the alias that was used.
      via: star < 0 ? null : (usedReal.length ? usedReal[usedReal.length - 1].call : hops[star].call),
    };
  }

  function isDirect(p) {
    return parsePath(p.path).direct;
  }

  // Station -> used digipeaters with a known position (path order) -> our hearing iGate.
  // digiPositions: { CALL: {latitude, longitude} }. Digis without a position of their own
  // beacon are left out of the line and reported in `missing`.
  function buildRoute(p, digiPositions) {
    var info = parsePath(p.path);
    var pos = digiPositions || {};
    var points = [];
    var stops = []; // same order as points: what sits at each vertex
    var missing = [];
    function push(lat, lon, kind, label) {
      var last = points[points.length - 1];
      if (last && last[0] === lat && last[1] === lon) return;
      points.push([lat, lon]);
      stops.push({ kind: kind, label: label });
    }
    if (hasPosition(p)) push(Number(p.latitude), Number(p.longitude), "station", p.source);
    info.hops.forEach(function (h) {
      if (!h.used || h.alias) return;
      var d = Object.prototype.hasOwnProperty.call(pos, h.call) ? pos[h.call] : null;
      if (d && hasPosition(d)) push(Number(d.latitude), Number(d.longitude), "digi", h.call);
      else if (missing.indexOf(h.call) < 0) missing.push(h.call);
    });
    var hasIgate = p.station_latitude !== null && p.station_latitude !== undefined &&
      p.station_longitude !== null && p.station_longitude !== undefined;
    if (hasIgate) push(Number(p.station_latitude), Number(p.station_longitude), "igate", p.station_name);
    return {
      points: points,
      stops: stops,
      missing: missing,
      direct: info.direct,
      via: info.via,
      hasIgate: hasIgate,
      drawable: hasPosition(p) && hasIgate && points.length >= 2,
    };
  }

  // Every packet copy of the same transmission (same source, same payload, within 30 s of
  // `p`): one entry per hearing iGate, newest copy per station.
  function hearings(p, packets) {
    var key = pathlessKey(p.raw_tnc2);
    var byStation = {};
    (packets || []).concat([p]).forEach(function (q) {
      if (q.source !== p.source || pathlessKey(q.raw_tnc2) !== key) return;
      if (Math.abs(ts(q) - ts(p)) > WINDOW_MS) return;
      var cur = byStation[q.station_name];
      if (!cur || ts(q) > ts(cur) || (ts(q) === ts(cur) && q.id === p.id)) byStation[q.station_name] = q;
    });
    return Object.keys(byStation).sort().map(function (k) { return byStation[k]; });
  }

  // APRS symbol "<table><code>" -> sprite cell. Table "/" primary, "\\" secondary,
  // [0-9A-Z] secondary with that overlay character (a-j are the compressed-format
  // spelling of 0-9). Anything else is unknown -> caller draws a fallback marker.
  function symbolInfo(symbol) {
    var s = String(symbol || "");
    if (s.length !== 2) return null;
    var table = s.charAt(0);
    var code = s.charCodeAt(1);
    if (code < 33 || code > 126) return null;
    var overlay = null;
    var sheet;
    if (table === "/") sheet = 0;
    else if (table === "\\") sheet = 1;
    else {
      if (table >= "a" && table <= "j") table = String.fromCharCode(table.charCodeAt(0) - 49);
      if (!/^[0-9A-Z]$/.test(table)) return null;
      sheet = 1;
      overlay = table;
    }
    var idx = code - 33;
    var cell = { sheet: sheet, col: idx % 16, row: Math.floor(idx / 16), overlay: null };
    if (overlay !== null) {
      var o = overlay.charCodeAt(0) - 33;
      cell.overlay = { col: o % 16, row: Math.floor(o / 16) };
    }
    return cell;
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
    parsePath: parsePath,
    isDirect: isDirect,
    isAlias: isAlias,
    buildRoute: buildRoute,
    hearings: hearings,
    symbolInfo: symbolInfo,
    pathlessKey: pathlessKey,
  };
});
