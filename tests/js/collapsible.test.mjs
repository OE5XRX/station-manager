// Collapsible control-page sections (static/js/collapsible.js).
// Run: node tests/js/collapsible.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));

const docListeners = {};
globalThis.window = {};
globalThis.document = {
  addEventListener: (n, fn) => {
    (docListeners[n] = docListeners[n] || []).push(fn);
  },
};
const registered = {};
window.Alpine = { data: (name, fn) => (registered[name] = fn) };

const { cpCollapsible, PREFIX } = require(path.resolve(here, "../../static/js/collapsible.js"));
docListeners["alpine:init"].forEach((fn) => fn());

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

function memStorage(data = {}) {
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => (data[k] = String(v)),
  };
}

ok("key prefix is cp-collapse:", () => {
  assert.equal(PREFIX, "cp-collapse:");
});

ok("collapsed by default", () => {
  assert.equal(cpCollapsible("sstv", memStorage()).open, false);
  assert.equal(cpCollapsible("sstv", null).open, false);
});

ok("restores the stored state per key", () => {
  const s = memStorage({ "cp-collapse:sstv": "1", "cp-collapse:scanner": "0" });
  assert.equal(cpCollapsible("sstv", s).open, true);
  assert.equal(cpCollapsible("scanner", s).open, false);
  assert.equal(cpCollapsible("repeaters", s).open, false);
});

ok("toggle flips and persists", () => {
  const s = memStorage();
  const c = cpCollapsible("repeaters", s);
  c.toggle();
  assert.equal(c.open, true);
  assert.equal(s.data["cp-collapse:repeaters"], "1");
  c.toggle();
  assert.equal(c.open, false);
  assert.equal(s.data["cp-collapse:repeaters"], "0");
  assert.equal(cpCollapsible("repeaters", s).open, false);
});

ok("a broken storage never breaks the toggle", () => {
  const bad = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("quota");
    },
  };
  const c = cpCollapsible("sstv", bad);
  assert.equal(c.open, false);
  c.toggle();
  assert.equal(c.open, true);
});

ok("registered as Alpine.data cpCollapsible(key) on window.localStorage", () => {
  const data = {};
  window.localStorage = memStorage(data);
  const c = registered.cpCollapsible("scanner");
  assert.equal(c.open, false);
  c.toggle();
  assert.equal(data["cp-collapse:scanner"], "1");
  assert.equal(registered.cpCollapsible("scanner").open, true);
});

console.log(`# ${passed} passed`);
