// Regression: template expressions must resolve to the controlPanel methods inside
// Alpine's merged scope. Alpine 3 evaluates every expression against
// mergeProxies([scope, ...dataStack]) where `scope` is a plain `{}`; its `get` trap
// picks the FIRST object for which `Reflect.has(obj, name)` is true -- and a plain
// object "has" every Object.prototype member. A component method named like one
// (the old `valueOf`) is therefore shadowed by Object.prototype.valueOf, which
// returns the scope proxy itself (truthy): bool toggles always rendered ON and a
// click always sent `!proxy` === false (bug: FM filter toggles could not be
// switched; enum <option :selected> never matched either).
// Run: node tests/js/control-panel-scope.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import fs from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../..");

const listeners = {};
globalThis.window = {};
globalThis.document = {
  addEventListener: (n, fn) => {
    listeners[n] = fn;
  },
};
window.OE5XRXControlLogic = require(path.join(root, "static/js/control-logic.js"));
require(path.join(root, "static/js/control-panel.js"));

let factory = null;
window.Alpine = {
  store: () => ({}),
  data: (name, fn) => {
    if (name === "controlPanel") factory = fn;
  },
};
listeners["alpine:init"]();
assert.ok(factory, "controlPanel registered");

// Same lookup semantics as Alpine 3.14 mergeProxies (has/get traps).
function alpineScope(objects) {
  return new Proxy(
    { objects },
    {
      has({ objects }, name) {
        if (name == Symbol.unscopables) return false;
        return objects.some(
          (obj) => Object.prototype.hasOwnProperty.call(obj, name) || Reflect.has(obj, name)
        );
      },
      get({ objects }, name, thisProxy) {
        return Reflect.get(objects.find((obj) => Reflect.has(obj, name)) || {}, name, thisProxy);
      },
    }
  );
}

// Every identifier the control templates call inside an Alpine attribute.
function templateCalls() {
  const dir = path.join(root, "apps/control/templates/control");
  const names = new Set();
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (p.endsWith(".html")) {
        const text = fs.readFileSync(p, "utf8");
        for (const attr of text.matchAll(/(?:x-[a-z-]+|[@:][a-zA-Z.-]+)="([^"]*)"/g)) {
          for (const call of attr[1].matchAll(/(?<![\w.$])([A-Za-z_]\w*)\s*\(/g)) {
            names.add(call[1]);
          }
        }
      }
    }
  };
  walk(dir);
  return names;
}

let passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

const c = factory();
const caps = [{ kind: "setting", name: "filter_hpf", type: "bool" }];
c._ingestInventory([
  { slot: "3", modules: [{ module: "fm", capabilities: caps, state: { filter_hpf: false } }] },
]);
const scope = alpineScope([{}, c]);

ok("bool toggle reads the real value through the Alpine scope", () => {
  assert.equal(scope.capValue("3", "fm", "filter_hpf"), false);
  c.values["3 fm filter_hpf"] = true;
  assert.equal(scope.capValue("3", "fm", "filter_hpf"), true);
  c.values["3 fm filter_hpf"] = false;
});

ok("a toggle click from OFF sends value true", () => {
  const sent = [];
  c._isOpen = () => true;
  c._send = (obj) => {
    sent.push(obj);
    return true;
  };
  c._widgetEl = () => null;
  c._capType = () => "bool";
  // Exactly the _bool.html @click expression, evaluated in the merged scope.
  scope.setValue("3", "fm", "filter_hpf", !scope.capValue("3", "fm", "filter_hpf"));
  assert.equal(sent.length, 1);
  assert.equal(sent[0].value, true);
});

ok("every method the control templates call resolves to the component", () => {
  const calls = [...templateCalls()].filter((n) => typeof c[n] === "function");
  assert.ok(calls.includes("capValue"), "templates call capValue");
  for (const name of calls) {
    assert.equal(scope[name], c[name], `template call '${name}' is shadowed in the Alpine scope`);
  }
});

ok("no controlPanel member shadows an Object.prototype name", () => {
  const reserved = new Set(Object.getOwnPropertyNames(Object.prototype));
  const clash = Object.keys(c).filter((k) => reserved.has(k));
  assert.deepEqual(clash, []);
});

console.log(`\n${passed} passed`);
