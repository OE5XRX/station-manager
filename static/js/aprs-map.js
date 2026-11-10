/* OE5XRX APRS page — DOM + Leaflet glue. Pure logic lives in aprs-logic.js.
   Every packet field is untrusted radio data: only textContent and DOM nodes. */
(function () {
  "use strict";
  var root = document.getElementById("aprs-root");
  if (!root || !window.L || !window.OE5XRXAprsLogic) return;
  var Logic = window.OE5XRXAprsLogic;
  var POLL_MS = 5000;
  var MAX_KEEP = 2000;
  var OLD_S = 3600;

  var stationSel = document.getElementById("aprs-station");
  var hoursSel = document.getElementById("aprs-hours");
  var statusEl = document.getElementById("aprs-status");
  var logBody = document.getElementById("aprs-log");
  var logEmpty = document.getElementById("aprs-log-empty");
  var igatesBody = document.getElementById("aprs-igates");

  var packets = [];
  var positions = [];
  var MAX_ROWS = 300;
  var lastId = 0;
  var timer = null;
  var inflight = null; // generation of the request in flight
  var generation = 0;

  var map = L.map("aprs-map", { worldCopyJump: true }).setView([47.6, 14.1], 7);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "© OpenStreetMap contributors",
  }).addTo(map);
  var markers = L.layerGroup().addTo(map);

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function panelVisible(name) {
    var p = root.querySelector('[data-tab-panel="' + name + '"]');
    return !!p && !p.hidden;
  }

  function ageS(iso) {
    return Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  }

  var markerBySource = {};

  // Diff against existing markers so an open popup survives a poll.
  function renderMap() {
    var latest = Logic.latestPositions(packets.concat(positions));
    Object.keys(markerBySource).forEach(function (src) {
      if (!latest.has(src)) {
        markers.removeLayer(markerBySource[src].marker);
        delete markerBySource[src];
      }
    });
    latest.forEach(function (p, source) {
      var old = ageS(p.heard_at) > OLD_S;
      var cur = markerBySource[source];
      if (cur && cur.id === p.id && cur.old === old) return;
      if (cur) markers.removeLayer(cur.marker);
      var tag = el("div", "aprs-tag" + (old ? " is-old" : ""), source);
      var icon = L.divIcon({ className: "", html: tag, iconSize: null });
      var m = L.marker([p.latitude, p.longitude], { icon: icon, title: source, keyboard: false });
      var pop = el("div");
      pop.appendChild(el("strong", "", source));
      pop.appendChild(el("div", "", Logic.formatAge(ageS(p.heard_at)) + " · " + p.station_name));
      if (p.comment) pop.appendChild(el("div", "", p.comment));
      m.bindPopup(pop);
      markers.addLayer(m);
      markerBySource[source] = { marker: m, id: p.id, old: old };
    });
  }

  function renderLog() {
    var groups = Logic.groupDuplicates(packets.slice(0, MAX_ROWS * 2)).slice(0, MAX_ROWS);
    logBody.textContent = "";
    groups.forEach(function (g) {
      var p = g.packet;
      var tr = el("tr");
      tr.appendChild(el("td", "aprs-heard", new Date(p.heard_at).toLocaleTimeString("de-AT") + " (" + Logic.formatAge(ageS(p.heard_at)) + ")"));
      tr.appendChild(el("td", "t-mono-sm", p.source));
      tr.appendChild(el("td", "", p.packet_type));
      tr.appendChild(el("td", "aprs-log-raw", p.raw_tnc2));
      var heard = g.stations.length > 1 ? "gehört von " + g.stations.join(", ") : p.station_name;
      tr.appendChild(el("td", "", heard));
      logBody.appendChild(tr);
    });
    logEmpty.hidden = groups.length > 0;
  }

  function buildUrl(afterId) {
    var q = "?hours=" + encodeURIComponent(hoursSel.value);
    if (stationSel.value) q += "&station=" + encodeURIComponent(stationSel.value);
    if (afterId) q += "&after_id=" + afterId;
    return root.dataset.packetsUrl + q;
  }

  function pollPackets(reset) {
    if (inflight === generation) return;
    var gen = generation;
    inflight = gen;
    fetch(buildUrl(reset ? 0 : lastId), { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then(function (data) {
        if (gen !== generation) return;
        if (reset) {
          packets = [];
          positions = data.positions || [];
          Object.keys(markerBySource).forEach(function (k) {
            markers.removeLayer(markerBySource[k].marker);
          });
          markerBySource = {};
        }
        if (data.packets.length) {
          packets = data.packets.concat(packets).slice(0, MAX_KEEP);
        }
        lastId = Math.max(lastId, data.last_id || 0);
        statusEl.textContent = "";
        renderMap();
        renderLog();
      })
      .catch(function () {
        statusEl.textContent = "Aktualisierung fehlgeschlagen, neuer Versuch folgt.";
      })
      .then(function () { if (inflight === gen) inflight = null; });
  }

  var STATE_LABEL = {
    running: ["pill-online", "Läuft"],
    idle: ["pill-muted", "Bereit"],
    in_use: ["pill-accent", "In Benutzung"],
    error: ["pill-error", "Fehler"],
    not_opted_in: ["pill-muted", "Nicht aktiviert"],
    agent_offline: ["pill-offline", "Agent offline"],
  };
  var WARNING = {
    no_igate_call: "Kein iGate-Rufzeichen: wird nicht ins APRS-IS gegated.",
    no_coordinates: "Keine Koordinaten: wird nicht ins APRS-IS gegated.",
  };

  function renderIgates(rows) {
    igatesBody.textContent = "";
    rows.forEach(function (r) {
      var tr = el("tr");
      tr.appendChild(el("td", "", r.name));
      tr.appendChild(el("td", "t-mono-sm", r.call || "–"));
      var st = el("td");
      var meta = STATE_LABEL[r.state] || ["pill-muted", r.state];
      var pill = el("span", "pill " + meta[0]);
      pill.appendChild(el("span", "dot"));
      pill.appendChild(document.createTextNode(meta[1]));
      st.appendChild(pill);
      if (r.reason) st.appendChild(el("div", "aprs-heard", r.reason));
      if (r.warning) st.appendChild(el("div", "aprs-warn", WARNING[r.warning] || r.warning));
      tr.appendChild(st);
      tr.appendChild(el("td", "aprs-heard", r.last_packet_at ? Logic.formatAge(ageS(r.last_packet_at)) : "–"));
      tr.appendChild(el("td", "num", String(r.packets_24h)));
      igatesBody.appendChild(tr);
    });
  }

  function pollIgates() {
    fetch(root.dataset.igatesUrl, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error(String(r.status))); })
      .then(renderIgates)
      .catch(function () {});
  }

  function tick() {
    if (document.hidden) return;
    if (panelVisible("karte")) pollPackets(false);
    if (panelVisible("igates")) pollIgates();
  }

  function start() {
    if (timer) return;
    timer = setInterval(tick, POLL_MS);
  }

  function reload() {
    generation += 1;
    inflight = null;
    lastId = 0;
    pollPackets(true);
  }

  stationSel.addEventListener("change", reload);
  hoursSel.addEventListener("change", reload);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();
  });
  root.addEventListener("click", function (e) {
    var tab = e.target.closest("[data-tab]");
    if (!tab) return;
    // app.js flips panel visibility first (same click); run after it.
    setTimeout(function () {
      if (tab.dataset.tab === "karte") { map.invalidateSize(); pollPackets(false); }
      if (tab.dataset.tab === "igates") pollIgates();
    }, 0);
  });

  var hash = (location.hash || "").slice(1);
  if (hash) {
    var t = root.querySelector('[data-tab="' + hash + '"]');
    if (t) t.click();
  }
  pollPackets(true);
  start();
})();
