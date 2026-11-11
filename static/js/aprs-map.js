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

  // Embedded on a station page (data-station-id): fixed station, last 24 h, no filters and
  // no iGates table; the surrounding tab panel decides when to poll.
  var fixedStation = root.dataset.stationId || "";
  var embeddedPanel = fixedStation ? root.closest("[data-tab-panel]") : null;
  var stationSel = document.getElementById("aprs-station") || { value: fixedStation, addEventListener: function () {} };
  var hoursSel = document.getElementById("aprs-hours") || { value: "24", addEventListener: function () {} };
  var statusEl = document.getElementById("aprs-status");
  var logBody = document.getElementById("aprs-log");
  var logEmpty = document.getElementById("aprs-log-empty");
  var igatesBody = document.getElementById("aprs-igates");
  var embeddedTab = embeddedPanel ? embeddedPanel.getAttribute("data-tab-panel") : null;

  var packets = [];
  var positions = [];
  var MAX_ROWS = 300;
  var lastId = 0;
  var loaded = false;
  var timer = null;
  var inflight = null; // generation of the request in flight
  var generation = 0;

  var map = L.map("aprs-map", { worldCopyJump: true }).setView([47.6, 14.1], 7);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    // OSM's tile policy requires a Referer; Django's same-origin policy would suppress it.
    referrerPolicy: "strict-origin-when-cross-origin",
    attribution: "© OpenStreetMap contributors",
  }).addTo(map);
  // Static, trusted constant (never packet data). The OSM attribution above must stay.
  map.attributionControl.addAttribution(
    'APRS symbols: <a href="https://github.com/hessu/aprs-symbols" target="_blank" rel="noopener noreferrer">hessu/aprs-symbols</a>'
  );
  var markers = L.layerGroup().addTo(map);

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function panelVisible(name) {
    if (embeddedPanel) return name === "karte" && !embeddedPanel.hidden;
    var p = root.querySelector('[data-tab-panel="' + name + '"]');
    return !!p && !p.hidden;
  }

  function ageS(iso) {
    return Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  }

  var markerBySource = {};
  var digiPositions = {};
  var directOnlyEl = document.getElementById("aprs-direct-only");
  var routeLayer = L.layerGroup().addTo(map);
  var selectedSource = null;
  // Sprite sheet URLs come from {% static %} (hashed names under WhiteNoise manifest storage).
  // Attribute names end in a letter: dataset camelCases "-<lowercase>" only, so "data-symbols-0"
  // would be dataset["symbols-0"] and the camelCase lookup undefined (-> url(undefined), no symbols).
  var SHEETS = [root.dataset.symbolsSheetA, root.dataset.symbolsSheetB, root.dataset.symbolsSheetC];

  function directOnly() {
    return !!(directOnlyEl && directOnlyEl.checked);
  }

  // 24 px cell of a 48 px/cell @2x sprite sheet (background-size is set in CSS).
  function spriteLayer(cls, sheet, col, row) {
    var n = el("span", cls);
    n.style.backgroundImage = "url(" + SHEETS[sheet] + ")";
    n.style.backgroundPosition = (-col * 24) + "px " + (-row * 24) + "px";
    return n;
  }

  // Standard APRS symbol with overlay character; unknown symbol -> plain dot.
  function symbolNode(symbol) {
    var info = Logic.symbolInfo(symbol);
    // A missing sheet URL must never become url(undefined): fall back to the plain dot.
    if (!info || !SHEETS[info.sheet] || (info.overlay && !SHEETS[2])) return el("span", "aprs-sym aprs-sym-unknown");
    var n = spriteLayer("aprs-sym", info.sheet, info.col, info.row);
    if (info.overlay) n.appendChild(spriteLayer("aprs-sym-overlay", 2, info.overlay.col, info.overlay.row));
    return n;
  }

  function pinNode(p, source, old, selected) {
    var pin = el("div", "aprs-pin" + (Logic.isDirect(p) ? "" : " is-digi") + (old ? " is-old" : "") + (selected ? " is-selected" : ""));
    pin.appendChild(symbolNode(p.symbol));
    pin.appendChild(el("span", "aprs-call", source));
    return pin;
  }

  function row(dl, label, value) {
    dl.appendChild(el("dt", "", label));
    var dd = el("dd");
    if (value instanceof Node) dd.appendChild(value);
    else dd.textContent = value;
    dl.appendChild(dd);
  }

  function pathNode(p) {
    var info = Logic.parsePath(p.path);
    var wrap = el("span");
    var box = el("span", "aprs-pop-path");
    if (!info.hops.length) box.textContent = "–";
    info.hops.forEach(function (h, i) {
      if (i) box.appendChild(document.createTextNode(","));
      var cls = h.starred ? "is-star" : (h.used ? "is-used" : "is-unused");
      box.appendChild(el("span", cls, h.call + (h.starred ? "*" : "")));
    });
    wrap.appendChild(box);
    wrap.appendChild(el("span", "aprs-pop-via", info.direct ? "direkt gehört" : "über " + info.via));
    return wrap;
  }

  function popupContent(p, source) {
    var pop = el("div", "aprs-pop");
    var head = el("div", "aprs-pop-head");
    head.appendChild(symbolNode(p.symbol));
    var title = el("div");
    title.appendChild(el("div", "aprs-pop-call", source));
    title.appendChild(el("div", "aprs-pop-sub", p.symbol ? "Symbol " + p.symbol : "Kein Symbol"));
    head.appendChild(title);
    pop.appendChild(head);

    var hear = Logic.hearings(p, packets.concat(positions));
    var dl = el("dl");
    row(dl, "Zuletzt", new Date(p.heard_at).toLocaleString("de-AT") + " (vor " + Logic.formatAge(ageS(p.heard_at)) + ")");
    row(dl, "Gehört von", hear.map(function (h) { return h.station_name; }).join(", "));
    row(dl, "Pfad", pathNode(p));
    if (p.comment) row(dl, "Kommentar", p.comment);
    pop.appendChild(dl);

    var notes = [];
    hear.forEach(function (h) {
      var r = Logic.buildRoute(routeInput(p, h), digiPositions);
      if (r.missing.length) notes.push(h.station_name + ": Position unbekannt für " + r.missing.join(", ") + " – Linie ohne diese Hops.");
      if (!r.hasIgate) notes.push(h.station_name + " hat keine Koordinaten – keine Linie.");
    });
    notes.forEach(function (t) { pop.appendChild(el("div", "aprs-pop-note", t)); });

    var det = el("details");
    det.appendChild(el("summary", "", "Rohpaket (TNC2)"));
    det.appendChild(el("pre", "", p.raw_tnc2));
    pop.appendChild(det);
    return pop;
  }

  // One transmission, one iGate copy: position from the newest packet, path from the copy.
  function routeInput(p, h) {
    return Object.assign({}, h, { latitude: p.latitude, longitude: p.longitude, source: p.source });
  }

  // Route colors come from the design tokens (theme-aware), never raw literals.
  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function drawRoutes(p) {
    var cDirect = token("--accent"), cDigi = token("--warn"), cGate = token("--signal"), cRim = token("--on-accent-ink");
    routeLayer.clearLayers();
    Logic.hearings(p, packets.concat(positions)).forEach(function (h) {
      var r = Logic.buildRoute(routeInput(p, h), digiPositions);
      if (!r.drawable) return;
      L.polyline(r.points, {
        color: r.direct ? cDirect : cDigi, weight: 3, opacity: 0.9,
        dashArray: r.direct ? null : "8 6", interactive: false,
      }).addTo(routeLayer);
      r.points.forEach(function (pt, i) {
        var stop = r.stops[i];
        if (stop.kind === "station") return;
        var dot = L.circleMarker(pt, {
          radius: stop.kind === "digi" ? 5 : 6, weight: 2, color: cRim,
          fillColor: stop.kind === "digi" ? cDigi : cGate, fillOpacity: 1,
        }).addTo(routeLayer);
        dot.bindTooltip(el("span", "", (stop.kind === "digi" ? "Digi " : "iGate ") + stop.label));
      });
    });
  }

  // Diff against existing markers so an open popup survives a poll.
  function renderMap() {
    var latest = Logic.latestPositions(packets.concat(positions), { directOnly: directOnly() });
    Object.keys(markerBySource).forEach(function (src) {
      if (!latest.has(src)) {
        markers.removeLayer(markerBySource[src].marker);
        delete markerBySource[src];
        if (selectedSource === src) { selectedSource = null; routeLayer.clearLayers(); }
      }
    });
    latest.forEach(function (p, source) {
      var old = ageS(p.heard_at) > OLD_S;
      var cur = markerBySource[source];
      if (cur && cur.id === p.id && cur.old === old) return;
      var reopen = !!(cur && cur.marker.isPopupOpen());
      if (cur) markers.removeLayer(cur.marker);
      var icon = L.divIcon({ className: "", html: pinNode(p, source, old, selectedSource === source), iconSize: null });
      var m = L.marker([p.latitude, p.longitude], { icon: icon, title: source, keyboard: true });
      m.bindPopup(function () { return popupContent(p, source); }, { maxWidth: 320 });
      m.on("popupopen", function () {
        selectedSource = source;
        setSelected(m, true);
        drawRoutes(p);
      });
      m.on("popupclose", function () {
        setSelected(m, false);
        // Replacing a marker closes its popup right before it is reopened on the new one.
        setTimeout(function () {
          var c = markerBySource[source];
          if (selectedSource === source && !(c && c.marker.isPopupOpen())) {
            selectedSource = null;
            routeLayer.clearLayers();
          }
        }, 0);
      });
      markers.addLayer(m);
      markerBySource[source] = { marker: m, id: p.id, old: old, direct: Logic.isDirect(p), labeled: null };
      if (reopen) {
        // Carried over from the replaced marker: no map jump on every poll.
        var pop = m.getPopup();
        pop.options.autoPan = false;
        m.openPopup();
        pop.options.autoPan = true;
      }
    });
    applyLabels();
  }
  map.on("zoomend moveend", applyLabels);

  // Crowded map: drop the callsign chips (they overlap) and show the call as a tooltip on
  // hover/tap instead. Directly heard stations keep their label first.
  function applyLabels() {
    var bounds = map.getBounds();
    var items = Object.keys(markerBySource).map(function (src) {
      var c = markerBySource[src];
      return { source: src, direct: c.direct, inView: bounds.contains(c.marker.getLatLng()) };
    });
    var plan = Logic.labelPlan(items, map.getZoom());
    items.forEach(function (it) {
      var c = markerBySource[it.source];
      var show = plan.has(it.source);
      if (c.labeled === show) return;
      c.labeled = show;
      var node = c.marker.getElement && c.marker.getElement();
      var pin = node && node.querySelector(".aprs-pin");
      if (pin) pin.classList.toggle("is-nolabel", !show);
      if (show) c.marker.unbindTooltip();
      else c.marker.bindTooltip(el("span", "", it.source), { direction: "top", offset: [0, -12] });
    });
  }

  function setSelected(m, on) {
    var node = m.getElement && m.getElement();
    var pin = node && node.querySelector(".aprs-pin");
    if (pin) pin.classList.toggle("is-selected", on);
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
    if (directOnly()) q += "&direct=1";
    return root.dataset.packetsUrl + q;
  }

  function pollPackets(reset) {
    // Nothing loaded yet (e.g. the first load failed): load like a reset.
    if (!loaded) reset = true;
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
          digiPositions = {};
          selectedSource = null;
          routeLayer.clearLayers();
          Object.keys(markerBySource).forEach(function (k) {
            markers.removeLayer(markerBySource[k].marker);
          });
          markerBySource = {};
        }
        if (data.packets.length) {
          packets = data.packets.concat(packets).slice(0, MAX_KEEP);
        }
        var dp = data.digi_positions || {};
        Object.keys(dp).forEach(function (call) {
          var cur = digiPositions[call];
          if (!cur || Date.parse(dp[call].heard_at) >= Date.parse(cur.heard_at)) digiPositions[call] = dp[call];
        });
        lastId = Math.max(lastId, data.last_id || 0);
        loaded = true;
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
    if (!igatesBody) return;
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
    loaded = false;
    pollPackets(true);
  }

  stationSel.addEventListener("change", reload);
  hoursSel.addEventListener("change", reload);
  if (directOnlyEl) directOnlyEl.addEventListener("change", reload);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();
  });
  (embeddedPanel ? document : root).addEventListener("click", function (e) {
    var tab = e.target.closest("[data-tab]");
    if (!tab) return;
    // app.js flips panel visibility first (same click); run after it.
    setTimeout(function () {
      if (tab.dataset.tab === (embeddedTab || "karte")) { map.invalidateSize(); pollPackets(false); }
      if (tab.dataset.tab === "igates") pollIgates();
    }, 0);
  });

  var hash = (location.hash || "").slice(1);
  if (hash) {
    var t = root.querySelector('[data-tab="' + hash + '"]');
    if (t) t.click();
  }
  if (!embeddedPanel || !embeddedPanel.hidden) pollPackets(true);
  start();
})();
