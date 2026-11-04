// Behavioural test: a [data-tab-link] click activates the target tab of the
// page's tab group and moves keyboard focus to that tab button.
// Run: node tests/js/app-tab-link.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = readFileSync(path.resolve(here, "../../static/js/app.js"), "utf8");

const calls = [];
const tab = {
  click: () => calls.push("click"),
  focus: () => calls.push("focus"),
  scrollIntoView: () => calls.push("scroll"),
};
const listeners = {};
const on = (n, fn) => {
  (listeners[n] ||= []).push(fn);
};
const document = {
  hidden: false,
  addEventListener: on,
  dispatchEvent: () => true,
  querySelector: (sel) => (sel === '[data-tabs] [data-tab="metrics"]' ? tab : null),
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

const link = { getAttribute: (a) => (a === "data-tab-link" ? "metrics" : null) };
const target = { closest: (sel) => (sel === "[data-tab-link]" ? link : null) };
let prevented = false;
const event = { target, preventDefault: () => (prevented = true) };
for (const fn of listeners["click"] || []) fn(event);

assert.equal(prevented, true, "tab-link click is handled");
assert.ok(calls.includes("click"), "target tab activated");
assert.ok(calls.includes("focus"), "focus moved to the target tab");
assert.ok(calls.indexOf("focus") > calls.indexOf("click"), "focus after activation");

console.log("app-tab-link: ok");
