// Behavioural test: app.js cancels htmx requests of elements marked
// data-poll-visible-only while the tab is hidden (no eval'd trigger filter, so
// it holds under a CSP without unsafe-eval). Loads static/js/app.js in a vm
// sandbox against minimal DOM stubs.
// Run: node tests/js/app-poll-visibility.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = readFileSync(path.resolve(here, "../../static/js/app.js"), "utf8");

const listeners = {};
const on = (n, fn) => {
  (listeners[n] ||= []).push(fn);
};
const document = {
  hidden: false,
  addEventListener: on,
  dispatchEvent: () => true,
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
  documentElement: { setAttribute() {}, getAttribute: () => "dark" },
  body: { addEventListener: on },
};
vm.runInNewContext(src, {
  document,
  window: {},
  location: { hash: "" },
  history: {},
  localStorage: { getItem: () => null, setItem() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  CustomEvent: class {},
  setTimeout,
  setInterval,
});

const handlers = listeners["htmx:beforeRequest"] || [];
assert.equal(handlers.length, 1, "one htmx:beforeRequest listener registered");

function fire(marked) {
  const elt = { hasAttribute: (a) => marked && a === "data-poll-visible-only" };
  let prevented = false;
  handlers[0]({ detail: { elt }, target: elt, preventDefault: () => (prevented = true) });
  return prevented;
}

document.hidden = true;
assert.equal(fire(true), true, "hidden tab: marked poll is cancelled");
assert.equal(fire(false), false, "hidden tab: unmarked request is untouched");
document.hidden = false;
assert.equal(fire(true), false, "visible tab: marked poll runs");
assert.equal(fire(false), false, "visible tab: unmarked request runs");
// Defensive: an event without detail/elt must not throw.
handlers[0]({ preventDefault() {} });

console.log("app-poll-visibility: ok");
