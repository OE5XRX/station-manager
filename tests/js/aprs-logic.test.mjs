// Pure-logic tests for static/js/aprs-logic.js. Run: node tests/js/aprs-logic.test.mjs
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const L = createRequire(import.meta.url)(path.resolve(here, "../../static/js/aprs-logic.js"));

const pkt = (id, station, iso, raw, extra = {}) => ({
  id, station_name: station, heard_at: iso, raw_tnc2: raw, source: "OE5ABC-9",
  latitude: null, longitude: null, ...extra,
});

{
  const a = pkt(1, "-211", "2026-11-10T10:00:00Z", "OE5ABC-9>APRS,WIDE1-1:!4812.00N/01400.00E>hi");
  const b = pkt(2, "-205", "2026-11-10T10:00:20Z", "OE5ABC-9>APRS,DL0XY*,WIDE1-1:!4812.00N/01400.00E>hi");
  const g = L.groupDuplicates([a, b]);
  assert.equal(g.length, 1, "same packet within 30 s merges (paths ignored)");
  assert.deepEqual(g[0].stations.sort(), ["-205", "-211"]);
  assert.equal(g[0].packet.id, 2, "anchored at newest");
}
{
  const a = pkt(1, "-211", "2026-11-10T10:00:00Z", "X>APRS:>hi");
  const b = pkt(2, "-205", "2026-11-10T10:00:31Z", "X>APRS:>hi");
  assert.equal(L.groupDuplicates([a, b]).length, 2, "kept apart after 31 s");
}
{
  const a = pkt(1, "-211", "2026-11-10T10:00:00Z", "X>APRS:>hi");
  const b = pkt(2, "-211", "2026-11-10T10:00:05Z", "Y>APRS:>hi");
  assert.equal(L.groupDuplicates([a, b]).length, 2, "different payload never merges");
}
{
  const old = pkt(1, "-211", "2026-11-10T10:00:00Z", "r", { latitude: 48.1, longitude: 14.0 });
  const nu = pkt(2, "-211", "2026-11-10T11:00:00Z", "r2", { latitude: 48.2, longitude: 14.1 });
  const none = pkt(3, "-211", "2026-11-10T12:00:00Z", "r3");
  const m = L.latestPositions([nu, old, none]);
  assert.equal(m.size, 1);
  assert.equal(m.get("OE5ABC-9").id, 2, "newest positioned packet wins, coordinate-less ignored");
}
assert.equal(L.formatAge(5), "5 s");
assert.equal(L.formatAge(125), "2 min");
assert.equal(L.formatAge(7300), "2 h");
assert.equal(L.formatAge(200000), "2 d");
console.log("aprs-logic: ok");

// --- path evaluation: direct vs. via digipeater ---
{
  const d = L.parsePath([]);
  assert.equal(d.direct, true);
  assert.equal(d.via, null);
  assert.equal(L.parsePath(["WIDE1-1", "WIDE2-1"]).direct, true, "no * = heard directly");
  assert.equal(L.parsePath(undefined).direct, true, "missing path tolerated");
  const v = L.parsePath(["DB0ABC*", "WIDE2-1"]);
  assert.equal(v.direct, false);
  assert.equal(v.via, "DB0ABC");
  assert.deepEqual(v.hops.map((h) => h.used), [true, false], "hops after * are unused");
  assert.deepEqual(v.hops.map((h) => h.alias), [false, true]);
  const two = L.parsePath(["DB0ABC", "DB0DEF*", "WIDE2-1"]);
  assert.equal(two.via, "DB0DEF", "last real digi is the one named");
  assert.deepEqual(two.hops.map((h) => h.used), [true, true, false], "earlier hops count as used");
  const al = L.parsePath(["WIDE1*", "WIDE2-1"]);
  assert.equal(al.via, "WIDE1", "only an alias used: name the alias");
  assert.equal(L.isAlias("TCPIP"), true);
  assert.equal(L.isAlias("DB0ABC-5"), false);
  assert.equal(L.isAlias("RELAY"), true);
}
{
  const a = pkt(1, "-211", "2026-11-10T10:00:00Z", "A>APRS:!x", { source: "A", latitude: 1, longitude: 1, path: [] });
  const b = pkt(2, "-211", "2026-11-10T10:00:10Z", "A>APRS,D*:!x", { source: "A", latitude: 2, longitude: 2, path: ["D*"] });
  assert.equal(L.latestPositions([a, b]).get("A").id, 2);
  assert.equal(L.latestPositions([a, b], { directOnly: true }).get("A").id, 1, "direct-only skips relayed copy");
  assert.equal(L.latestPositions([b], { directOnly: true }).size, 0);
}

// --- route line: station -> known digis in path order -> iGate ---
{
  const p = pkt(1, "-211", "2026-11-10T10:00:00Z", "S>APRS,D1,D2*,WIDE2-1:!x", {
    source: "S", latitude: 48, longitude: 14, station_latitude: 47, station_longitude: 13,
    path: ["D1", "D2*", "WIDE2-1"],
  });
  const full = L.buildRoute(p, { D1: { latitude: 48.5, longitude: 14.5 }, D2: { latitude: 47.5, longitude: 13.5 } });
  assert.deepEqual(full.points, [[48, 14], [48.5, 14.5], [47.5, 13.5], [47, 13]]);
  assert.deepEqual(full.stops.map((x) => x.kind + ":" + x.label), ["station:S", "digi:D1", "digi:D2", "igate:-211"]);
  assert.deepEqual(full.missing, []);
  assert.equal(full.drawable, true);
  assert.equal(full.via, "D2");
  const gap = L.buildRoute(p, { D2: { latitude: 47.5, longitude: 13.5 } });
  assert.deepEqual(gap.points, [[48, 14], [47.5, 13.5], [47, 13]], "unknown digi: segment dropped");
  assert.deepEqual(gap.missing, ["D1"]);
  const none = L.buildRoute(p, {});
  assert.deepEqual(none.points, [[48, 14], [47, 13]]);
  assert.deepEqual(none.missing, ["D1", "D2"]);
  assert.deepEqual(L.buildRoute(p, null).missing, ["D1", "D2"], "null digi map tolerated");
  const proto = L.buildRoute(p, {});
  assert.equal(L.buildRoute({ ...p, path: ["constructor*"] }, {}).missing[0], "constructor", "no prototype lookups");
  assert.ok(proto.points.length === 2);
}
{
  const direct = pkt(1, "-211", "2026-11-10T10:00:00Z", "S>APRS:!x", {
    source: "S", latitude: 48, longitude: 14, station_latitude: 47, station_longitude: 13, path: [],
  });
  const r = L.buildRoute(direct, {});
  assert.deepEqual(r.points, [[48, 14], [47, 13]]);
  assert.equal(r.direct, true);
  const noGate = L.buildRoute({ ...direct, station_latitude: null, station_longitude: null }, {});
  assert.equal(noGate.drawable, false, "iGate without coordinates: nothing to draw");
  assert.equal(noGate.hasIgate, false);
  const same = L.buildRoute({ ...direct, latitude: 47, longitude: 13 }, {});
  assert.equal(same.drawable, false, "coincident points collapse");
}

// --- who heard it ---
{
  const a = pkt(1, "-211", "2026-11-10T10:00:00Z", "S>APRS,D*:!x", { source: "S" });
  const b = pkt(2, "-205", "2026-11-10T10:00:20Z", "S>APRS:!x", { source: "S" });
  const c = pkt(3, "-300", "2026-11-10T10:05:00Z", "S>APRS:!x", { source: "S" });
  const o = pkt(4, "-400", "2026-11-10T10:00:05Z", "S>APRS:!y", { source: "S" });
  assert.deepEqual(L.hearings(b, [a, b, c, o]).map((x) => x.station_name), ["-205", "-211"]);
  assert.deepEqual(L.hearings(b, []).map((x) => x.station_name), ["-205"]);
}

// --- symbols ---
{
  assert.deepEqual(L.symbolInfo("/>"), { sheet: 0, col: 13, row: 1, overlay: null }, "car");
  assert.deepEqual(L.symbolInfo("/!"), { sheet: 0, col: 0, row: 0, overlay: null });
  assert.deepEqual(L.symbolInfo("\\#"), { sheet: 1, col: 2, row: 0, overlay: null });
  const ov = L.symbolInfo("D#");
  assert.equal(ov.sheet, 1);
  assert.deepEqual(ov.overlay, { col: ("D".charCodeAt(0) - 33) % 16, row: Math.floor(("D".charCodeAt(0) - 33) / 16) });
  assert.equal(L.symbolInfo("a#").overlay.col, ("0".charCodeAt(0) - 33) % 16, "compressed a-j = overlay 0-9");
  assert.equal(L.symbolInfo(""), null);
  assert.equal(L.symbolInfo("/"), null);
  assert.equal(L.symbolInfo("x#"), null, "lowercase outside a-j: unknown table");
  assert.equal(L.symbolInfo("/\x7f"), null, "code out of range");
  assert.equal(L.symbolInfo("/ "), null);
}
console.log("aprs-logic route/symbol tests ok");
