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
