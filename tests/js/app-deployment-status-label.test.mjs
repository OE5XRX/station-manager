// Behavioural test: live deployment pills use the server-translated
// ``status_label`` of the WS payload and fall back to the raw code.
// Run: node tests/js/app-deployment-status-label.test.mjs  (exit 0 = pass).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = readFileSync(path.resolve(here, "../../static/js/app.js"), "utf8");

const pill = { className: "", textContent: "" };
const resultPill = { className: "", textContent: "" };
const deployment = {
  querySelector: (sel) => (sel === "[data-deployment-status]" ? pill : null),
};
const resultRow = {
  querySelector: (sel) => (sel === "[data-result-status]" ? resultPill : null),
};
const sockets = [];
class FakeWebSocket {
  constructor() {
    this.handlers = {};
    sockets.push(this);
  }
  addEventListener(name, fn) {
    this.handlers[name] = fn;
  }
  close() {}
}
const ready = [];
const on = (n, fn) => {
  if (n === "DOMContentLoaded") ready.push(fn);
};
const document = {
  hidden: false,
  addEventListener: on,
  dispatchEvent: () => true,
  querySelector: (sel) => {
    if (sel === "[data-ws-deployments]") return {};
    if (sel === '[data-deployment-id="7"]') return deployment;
    if (sel === '[data-result-id="9"]') return resultRow;
    return null;
  },
  querySelectorAll: () => [],
  getElementById: () => null,
  documentElement: { setAttribute() {}, getAttribute: () => "dark" },
  body: { addEventListener: on },
};
vm.runInNewContext(src, {
  document,
  window: {},
  location: { hash: "", protocol: "http:", host: "x" },
  history: {},
  localStorage: { getItem: () => null, setItem() {} },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  CustomEvent: class {},
  WebSocket: FakeWebSocket,
  setTimeout,
  setInterval,
});
if (!sockets.length) ready.forEach((fn) => fn());
assert.equal(sockets.length, 1, "deployments WS opened");
const send = (payload) => sockets[0].handlers.message({ data: JSON.stringify(payload) });

// Server label wins, upper-cased.
send({
  deployment_id: 7,
  status: "in_progress",
  status_label: "Läuft",
  result: { id: 9, status: "rolled_back", status_label: "Zurückgerollt" },
});
assert.equal(pill.textContent, "LÄUFT");
assert.equal(pill.className, "pill pill-in-progress");
assert.equal(resultPill.textContent, "ZURÜCKGEROLLT");

// Older server without labels: readable fallback from the code, never undefined.
send({ deployment_id: 7, status: "in_progress", result: { id: 9, status: "rolled_back" } });
assert.equal(pill.textContent, "IN PROGRESS");
assert.equal(resultPill.textContent, "ROLLED BACK");

console.log("app-deployment-status-label: ok");
