/* OE5XRX Station Manager — client interactions
   All event handlers are delegated so templates stay declarative.
   CSP: no inline JS; this file loaded via <script src>. */

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Theme
  // ---------------------------------------------------------------------------
  var THEME_KEY = "oe5xrx.theme";
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch (_) {}
  }
  var savedTheme;
  try { savedTheme = localStorage.getItem(THEME_KEY); } catch (_) { savedTheme = null; }
  if (savedTheme === "light" || savedTheme === "dark") {
    applyTheme(savedTheme);
  } else {
    applyTheme("dark");
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-theme-toggle]");
    if (!t) return;
    var cur = document.documentElement.getAttribute("data-theme") || "dark";
    applyTheme(cur === "dark" ? "light" : "dark");
  });

  // ---------------------------------------------------------------------------
  // Sidebar (mobile)
  // ---------------------------------------------------------------------------
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-menu-toggle]");
    if (btn) {
      var sb = document.querySelector(".sidebar");
      if (sb) sb.classList.toggle("is-open");
      return;
    }
    var backdrop = e.target.closest(".sidebar-backdrop");
    if (backdrop) {
      var sb2 = document.querySelector(".sidebar");
      if (sb2) sb2.classList.remove("is-open");
    }
  });

  // ---------------------------------------------------------------------------
  // Flash auto-dismiss
  // ---------------------------------------------------------------------------
  function dismissFlash(el) {
    if (!el) return;
    el.style.transition = "opacity .3s, transform .3s";
    el.style.opacity = "0";
    el.style.transform = "translateX(20px)";
    setTimeout(function () { el.remove(); }, 320);
  }
  document.addEventListener("click", function (e) {
    var closeBtn = e.target.closest(".flash-close");
    if (closeBtn) dismissFlash(closeBtn.closest(".flash"));
  });
  document.querySelectorAll(".flash").forEach(function (el) {
    setTimeout(function () { dismissFlash(el); }, 7000);
  });

  // ---------------------------------------------------------------------------
  // Copy-to-clipboard
  // ---------------------------------------------------------------------------
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var targetSel = btn.getAttribute("data-copy");
    var target = document.querySelector(targetSel);
    if (!target) return;
    var text = target.innerText || target.textContent || "";
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { flashCopied(btn); });
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); flashCopied(btn); } catch (_) {}
      document.body.removeChild(ta);
    }
  });
  function flashCopied(btn) {
    var original = btn.textContent;
    btn.textContent = "COPIED";
    btn.classList.add("copied");
    setTimeout(function () { btn.textContent = original; btn.classList.remove("copied"); }, 1600);
  }

  // ---------------------------------------------------------------------------
  // Tabs (aria-driven)
  // ---------------------------------------------------------------------------
  document.addEventListener("click", function (e) {
    var tab = e.target.closest("[data-tab]");
    if (!tab) return;
    e.preventDefault();
    var name = tab.getAttribute("data-tab");
    var group = tab.closest("[data-tabs]");
    if (!group) return;
    group.querySelectorAll("[data-tab]").forEach(function (t) {
      t.classList.toggle("active", t === tab);
      t.setAttribute("aria-selected", t === tab ? "true" : "false");
    });
    var container = group.parentElement;
    var panels = container.querySelectorAll("[data-tab-panel]");
    panels.forEach(function (p) {
      p.hidden = p.getAttribute("data-tab-panel") !== name;
    });
    try { history.replaceState(null, "", "#" + name); } catch (_) {}
  });
  if (location.hash) {
    var h = location.hash.substring(1);
    var initial = document.querySelector('[data-tab="' + h + '"]');
    if (initial) initial.click();
  }

  // ---------------------------------------------------------------------------
  // Dropdown menu
  // ---------------------------------------------------------------------------
  document.addEventListener("click", function (e) {
    var trigger = e.target.closest("[data-menu-trigger]");
    if (trigger) {
      var m = trigger.closest(".menu");
      if (m) {
        document.querySelectorAll(".menu.is-open").forEach(function (x) { if (x !== m) x.classList.remove("is-open"); });
        m.classList.toggle("is-open");
      }
      return;
    }
    if (!e.target.closest(".menu-list")) {
      document.querySelectorAll(".menu.is-open").forEach(function (x) { x.classList.remove("is-open"); });
    }
  });

  // ---------------------------------------------------------------------------
  // Live UTC clock
  // ---------------------------------------------------------------------------
  var clock = document.querySelector("[data-utc-clock]");
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    if (!clock) return;
    var d = new Date();
    var utc = pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds());
    clock.textContent = utc;
  }
  if (clock) { tick(); setInterval(tick, 1000); }

  // ---------------------------------------------------------------------------
  // Stations WebSocket
  // ---------------------------------------------------------------------------
  function connectStationsWS() {
    var enabled = document.querySelector("[data-ws-stations]");
    if (!enabled) return;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + location.host + "/ws/stations/status/";
    var ws, retry = 0;
    function connect() {
      try { ws = new WebSocket(url); } catch (_) { scheduleReconnect(); return; }
      ws.addEventListener("open", function () { retry = 0; setBadge(true); });
      ws.addEventListener("close", function () { setBadge(false); scheduleReconnect(); });
      ws.addEventListener("error", function () { try { ws.close(); } catch (_) {} });
      ws.addEventListener("message", function (ev) {
        var data;
        try { data = JSON.parse(ev.data); } catch (_) { return; }
        applyStationUpdate(data);
      });
    }
    function scheduleReconnect() {
      retry += 1;
      var wait = Math.min(30000, 1000 * Math.pow(1.6, retry));
      setTimeout(connect, wait);
    }
    function setBadge(on) {
      document.querySelectorAll("[data-ws-indicator]").forEach(function (b) {
        b.classList.toggle("is-live", on);
        b.textContent = on ? "LIVE" : "RECONNECTING";
      });
    }
    connect();
  }
  function setPill(el, statusClass, labelText) {
    if (!el) return;
    el.className = "pill pill-" + statusClass;
    // Rebuild children safely (no innerHTML with dynamic data)
    while (el.firstChild) el.removeChild(el.firstChild);
    var dot = document.createElement("span");
    dot.className = "dot";
    el.appendChild(dot);
    el.appendChild(document.createTextNode(labelText));
  }
  function applyStationUpdate(data) {
    if (!data || !data.id) return;
    var row = document.querySelector('[data-station-id="' + Number(data.id) + '"]');
    if (!row) return;
    var statusCell = row.querySelector("[data-station-status]");
    if (statusCell) {
      var status = String(data.status || "offline");
      setPill(statusCell, status, status.toUpperCase());
    }
    var verCell = row.querySelector("[data-station-version]");
    if (verCell && data.current_os_version) verCell.textContent = data.current_os_version;
    var ipCell = row.querySelector("[data-station-ip]");
    if (ipCell && data.last_ip_address) ipCell.textContent = data.last_ip_address;
    var seenCell = row.querySelector("[data-station-seen]");
    if (seenCell && data.last_seen) seenCell.textContent = relativeTime(data.last_seen);
  }

  function relativeTime(iso) {
    try {
      var d = new Date(iso);
      var diff = (Date.now() - d.getTime()) / 1000;
      if (diff < 60) return Math.floor(diff) + "s ago";
      if (diff < 3600) return Math.floor(diff / 60) + "m ago";
      if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
      return Math.floor(diff / 86400) + "d ago";
    } catch (_) { return "-"; }
  }

  // ---------------------------------------------------------------------------
  // Deployments WebSocket
  // ---------------------------------------------------------------------------
  function connectDeploymentsWS() {
    var enabled = document.querySelector("[data-ws-deployments]");
    if (!enabled) return;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + location.host + "/ws/deployments/status/";
    var ws, retry = 0;
    function connect() {
      try { ws = new WebSocket(url); } catch (_) { scheduleReconnect(); return; }
      ws.addEventListener("open", function () { retry = 0; });
      ws.addEventListener("close", scheduleReconnect);
      ws.addEventListener("error", function () { try { ws.close(); } catch (_) {} });
      ws.addEventListener("message", function (ev) {
        var data;
        try { data = JSON.parse(ev.data); } catch (_) { return; }
        applyDeploymentUpdate(data);
      });
    }
    function scheduleReconnect() {
      retry += 1;
      var wait = Math.min(30000, 1000 * Math.pow(1.6, retry));
      setTimeout(connect, wait);
    }
    connect();
  }
  function applyDeploymentUpdate(data) {
    if (!data || !data.deployment_id) return;
    var d = document.querySelector('[data-deployment-id="' + Number(data.deployment_id) + '"]');
    if (!d) return;
    if (data.progress) {
      var bar = d.querySelector("[data-deployment-bar]");
      if (bar) bar.style.width = Number(data.progress.percentage) + "%";
      var pctLabel = d.querySelector("[data-deployment-pct]");
      if (pctLabel) pctLabel.textContent = data.progress.percentage + "%";
      var totals = d.querySelector("[data-deployment-totals]");
      if (totals) totals.textContent =
        data.progress.completed + " / " + data.progress.total + " OK · " + data.progress.failed + " FAIL";
    }
    if (data.status) {
      var s = d.querySelector("[data-deployment-status]");
      if (s) {
        var st = String(data.status).replace("_", "-");
        s.className = "pill pill-" + st;
        s.textContent = String(data.status).replace("_", " ").toUpperCase();
      }
    }
    if (data.result) {
      var row = document.querySelector('[data-result-id="' + Number(data.result.id) + '"]');
      if (row) {
        var sc = row.querySelector("[data-result-status]");
        if (sc) {
          sc.className = "pill pill-" + String(data.result.status);
          sc.textContent = String(data.result.status).toUpperCase().replace("_", " ");
        }
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Terminal (xterm.js)
  // ---------------------------------------------------------------------------
  function initTerminal() {
    var host = document.getElementById("xterm-container");
    if (!host || typeof Terminal === "undefined") return;
    var stationId = host.getAttribute("data-station-id");
    if (!stationId) return;

    var term = new Terminal({
      cursorBlink: true,
      fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
      fontSize: 13,
      theme: {
        background: "#000000",
        foreground: "#F5F7FA",
        cursor: "#FF8A3D",
        selection: "rgba(255, 138, 61, 0.3)",
      },
    });
    term.open(host);
    term.write("\x1b[90mConnecting to station #" + stationId + "...\x1b[0m\r\n");

    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var ws = new WebSocket(proto + "//" + location.host + "/ws/terminal/" + stationId + "/");

    ws.addEventListener("open", function () {
      term.write("\x1b[32m[ connected ]\x1b[0m\r\n");
    });
    ws.addEventListener("message", function (ev) {
      try {
        var payload = JSON.parse(ev.data);
        if (payload.type === "output") term.write(payload.data);
        else if (payload.type === "closed") term.write("\r\n\x1b[31m[ closed: " + (payload.reason || "") + " ]\x1b[0m");
      } catch (_) {}
    });
    ws.addEventListener("close", function (ev) {
      term.write("\r\n\x1b[33m[ disconnected: " + ev.code + " ]\x1b[0m");
    });
    term.onData(function (data) {
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: "input", data: data }));
    });
  }

  // ---------------------------------------------------------------------------
  // HTMX CSRF
  // ---------------------------------------------------------------------------
  if (document.body) {
    document.body.addEventListener("htmx:configRequest", function (ev) {
      var token = document.querySelector('[name="csrfmiddlewaretoken"]');
      if (token) ev.detail.headers["X-CSRFToken"] = token.value;
    });
  }

  // ---------------------------------------------------------------------------
  // Confirm prompt on forms/buttons with data-confirm
  // ---------------------------------------------------------------------------
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || !form.matches || !form.matches("[data-confirm]")) return;
    var msg = form.getAttribute("data-confirm");
    if (!window.confirm(msg)) e.preventDefault();
  });
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-confirm], a[data-confirm]");
    if (!btn) return;
    if (btn.closest("form[data-confirm]")) return; // handled above
    var msg = btn.getAttribute("data-confirm");
    if (!window.confirm(msg)) { e.preventDefault(); e.stopPropagation(); }
  });

  // ---------------------------------------------------------------------------
  // Language switcher + generic submit-on-change
  // ---------------------------------------------------------------------------
  document.addEventListener("change", function (e) {
    var sel = e.target.closest("[data-lang-select]");
    if (sel) {
      var form = sel.closest("[data-lang-form]");
      if (form) form.submit();
      return;
    }
    var soc = e.target.closest("[data-submit-on-change]");
    if (soc) {
      var f = soc.closest("form");
      if (f && typeof f.requestSubmit === "function") f.requestSubmit();
      else if (f) f.submit();
    }
  });

  // ---------------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    connectStationsWS();
    connectDeploymentsWS();
    initTerminal();
  });
})();
