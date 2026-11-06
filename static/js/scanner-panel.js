/* OE5XRX Scanner Panel — Alpine component (spec 2026-11-06 §6).
   Starts and steers the station's carrier scanner. Like the repeater panel it
   owns no socket: frames go out via the window event "control:send"
   ({scan_start, scan_control}), replies and the agent's scan_state come back as
   the re-emitted window events control:result / control:error / control:scan_state.
   The scan loop itself lives in the agent; this panel only shows its state.

   Pure helpers are exported (window.OE5XRXScannerPanel / module.exports) for the
   Node test suite. Style follows repeater-panel.js (ES5-ish IIFE). */

(function () {
  "use strict";

  var STORAGE_PREFIX = "oe5xrx.bandmap."; // shared with the repeater panel
  var CARRIER_CAP = "carrier";
  var LISTEN_RANGE = [0.2, 60]; // seconds, same bounds the agent enforces
  var HANG_RANGE = [0, 600];
  var LISTEN_DEFAULT = 2;
  var HANG_DEFAULT = 5;
  var RADIUS_DEFAULT = 50;
  var NOTICE_MS = 7000;
  var PENDING_GIVEUP_MS = 15000;
  var ACTIONS = { stop: true, skip: true, hold: true, resume: true };

  // -------------------------------------------------------------------------
  // Pure helpers
  // -------------------------------------------------------------------------
  // Slots with at least one online module that advertises ``carrier``.
  function carrierSlots(slots) {
    var out = [];
    if (!Array.isArray(slots)) return out;
    for (var i = 0; i < slots.length; i++) {
      var mods = (slots[i] && slots[i].modules) || [];
      for (var j = 0; j < mods.length; j++) {
        var m = mods[j];
        if (!m || m.online === false) continue;
        var caps = m.capabilities || [];
        for (var c = 0; c < caps.length; c++) {
          if (caps[c] && caps[c].name === CARRIER_CAP) {
            var slot = String(slots[i].slot);
            if (out.indexOf(slot) < 0) out.push(slot);
          }
        }
      }
    }
    return out;
  }

  // Number from an input; accepts a comma decimal (de_AT keyboards).
  function toNumber(raw) {
    if (typeof raw === "number") return isFinite(raw) ? raw : null;
    var s = String(raw === null || raw === undefined ? "" : raw).trim().replace(",", ".");
    if (!/^[+-]?(\d+\.?\d*|\.\d+)$/.test(s)) return null;
    var n = parseFloat(s);
    return isFinite(n) ? n : null;
  }

  function clamp(raw, lo, hi, fallback) {
    var n = toNumber(raw);
    if (n === null) return fallback;
    return Math.min(hi, Math.max(lo, n));
  }

  function loadBandmap(storage, stationId) {
    try {
      var raw = storage && storage.getItem(STORAGE_PREFIX + String(stationId));
      var parsed = raw ? JSON.parse(raw) : null;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
      var out = {};
      for (var band in parsed) {
        if (Object.prototype.hasOwnProperty.call(parsed, band) && typeof parsed[band] === "string") {
          out[band] = parsed[band];
        }
      }
      return out;
    } catch (_) {
      return {};
    }
  }

  var STATE_LABELS = {
    scanning: "Scanning",
    active: "Carrier",
    hang: "Waiting for reply",
    held: "Held",
  };

  var REASONS = {
    operator: "You stopped the scan.",
    replaced: "Another scan replaced this one.",
    disconnect: "The station connection was lost.",
    shutdown: "The station agent shut down.",
    module_update: "A module is being updated.",
    no_reachable_entries: "None of the channels could be reached. Check that the modules are online.",
    empty_scan_list: "The scan list has no channel this station can tune.",
    invalid_scan: "The station rejected the scan request.",
    internal_error: "The scan stopped because of an internal error.",
    ptt: "Held while you transmit.",
    manual: "Held because a module setting was changed by hand.",
  };

  var HELD_WHY = { ptt: "transmitting", manual: "manual change", operator: "by you" };

  function reasonText(code) {
    return Object.prototype.hasOwnProperty.call(REASONS, code) ? REASONS[code] : "The scan stopped.";
  }

  function errorText(code) {
    switch (code) {
      case "not_locked":
        return "Take control of the station first.";
      case "forbidden":
        return "Your membership level may not change these settings.";
      case "no_coordinates":
        return "This station has no coordinates, so a radius cannot be used.";
      case "empty_scan_list":
        return "Nothing to scan: no repeater in range or in the chosen lists fits this station's modules.";
      case "not_connected":
        return "The station connection is down. Wait for it to reconnect.";
      case "timeout":
        return "The station did not answer in time.";
      default:
        return "The scan could not be started.";
    }
  }

  function formatMHz(value) {
    var n = Number(value);
    return isFinite(n) ? n.toFixed(4) : "";
  }

  var logic = {
    carrierSlots: carrierSlots,
    toNumber: toNumber,
    clamp: clamp,
    loadBandmap: loadBandmap,
    reasonText: reasonText,
    errorText: errorText,
    formatMHz: formatMHz,
  };

  // -------------------------------------------------------------------------
  // Alpine component
  // -------------------------------------------------------------------------
  function scannerPanel() {
    return {
      stationId: null,
      listsUrl: null,
      slots: [],
      lists: [],
      listsError: false,
      useRadius: false,
      radiusKm: String(RADIUS_DEFAULT),
      selected: [], // list ids as strings (checkbox values)
      listenS: String(LISTEN_DEFAULT),
      hangS: String(HANG_DEFAULT),
      scan: null, // last non-stopped scan_state frame
      pending: false,
      notice: null,

      _reqSeq: 0,
      _requests: {},
      _listeners: [],
      _noticeTimer: null,
      _pendingTimer: null,
      _audioAnnounced: false,

      init: function () {
        var root = this.$el;
        this.stationId = root.getAttribute("data-station-id");
        this.listsUrl = root.getAttribute("data-lists-url");
        this._seedSlots();
        var self = this;
        this._on("control:inventory", function (e) {
          self.slots = (e.detail && e.detail.slots) || [];
        });
        this._on("control:scan_state", function (e) {
          self._onScanState(e.detail);
        });
        this._on("control:result", function (e) {
          self._onReply(e.detail);
        });
        this._on("control:error", function (e) {
          self._onReply(e.detail);
        });
        this.loadLists();
      },

      destroy: function () {
        for (var i = 0; i < this._listeners.length; i++) {
          window.removeEventListener(this._listeners[i].type, this._listeners[i].handler);
        }
        this._listeners = [];
        clearTimeout(this._noticeTimer);
        clearTimeout(this._pendingTimer);
      },

      _on: function (type, handler) {
        window.addEventListener(type, handler);
        this._listeners.push({ type: type, handler: handler });
      },

      _storage: function () {
        try {
          return window.localStorage;
        } catch (_) {
          return null;
        }
      },

      _seedSlots: function () {
        var el = document.getElementById("control-initial");
        if (!el) return;
        try {
          this.slots = JSON.parse(el.textContent);
        } catch (_) {
          this.slots = [];
        }
      },

      loadLists: function () {
        if (!this.listsUrl) return Promise.resolve();
        var self = this;
        return fetch(this.listsUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" })
          .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
          })
          .then(function (data) {
            self.lists = Array.isArray(data) ? data : [];
            self.listsError = false;
          })
          .catch(function () {
            self.listsError = true;
          });
      },

      // -- visibility / permissions ---------------------------------------------
      get visible() {
        return carrierSlots(this.slots).length > 0;
      },

      get canUse() {
        var s = window.Alpine && window.Alpine.store && window.Alpine.store("control");
        return !!(s && s.connected && s.youHold && s.canControl !== false);
      },

      // -- state rendering --------------------------------------------------------
      get running() {
        return this.scan !== null;
      },
      get held() {
        return !!this.scan && this.scan.state === "held";
      },
      get canHold() {
        return this.running && !this.held && this.canUse && !this.pending;
      },
      get canResume() {
        return this.held && this.canUse && !this.pending;
      },
      get canSkip() {
        return this.running && this.canUse && !this.pending;
      },
      get canStart() {
        return !this.running && this.canUse && !this.pending;
      },

      stateLabel: function () {
        if (!this.scan) return "";
        var base = STATE_LABELS[this.scan.state] || this.scan.state;
        var why = HELD_WHY[this.scan.reason];
        if (this.scan.state === "held" && why) return base + " (" + why + ")";
        return base;
      },

      // "2 / 6" — the server may omit the total, then only the position shows.
      position: function () {
        if (!this.scan || typeof this.scan.index !== "number") return "";
        var pos = String(this.scan.index + 1);
        return this.scan.count ? pos + " / " + this.scan.count : pos;
      },

      // Channel, frequency and module of the current step, whatever the frame names.
      where: function () {
        var s = this.scan;
        if (!s) return "";
        var parts = [];
        if (s.label) parts.push(s.label);
        if (s.rx_frequency !== undefined && s.rx_frequency !== null) parts.push(formatMHz(s.rx_frequency) + " MHz");
        if (s.module) parts.push(s.module);
        return parts.join(" · ");
      },

      reasonText: reasonText,

      toggleList: function (id) {
        var key = String(id);
        var at = this.selected.indexOf(key);
        if (at < 0) this.selected = this.selected.concat([key]);
        else this.selected = this.selected.filter(function (x) { return x !== key; });
      },

      // -- frames --------------------------------------------------------------------
      _requestId: function () {
        return "sc-" + Date.now().toString(36) + "-" + ++this._reqSeq;
      },

      _send: function (frame) {
        var self = this;
        this.pending = true;
        this._requests[frame.request_id] = true;
        clearTimeout(this._pendingTimer);
        this._pendingTimer = setTimeout(function () {
          self._onReply({ type: "error", request_id: frame.request_id, error: { code: "timeout" } });
        }, PENDING_GIVEUP_MS);
        window.dispatchEvent(new CustomEvent("control:send", { detail: frame }));
        return true;
      },

      start: function () {
        if (!this.canStart) return false;
        var radius = null;
        if (this.useRadius) {
          radius = toNumber(this.radiusKm);
          if (radius === null || radius <= 0) {
            this._notify({ ok: false, text: "Enter a radius greater than 0 km." });
            return false;
          }
        }
        var ids = [];
        for (var i = 0; i < this.selected.length; i++) ids.push(parseInt(this.selected[i], 10));
        if (radius === null && !ids.length) {
          this._notify({ ok: false, text: "Choose a radius or at least one scan list." });
          return false;
        }
        this._audioAnnounced = false;
        return this._send({
          type: "scan_start",
          request_id: this._requestId(),
          radius_km: radius,
          list_ids: ids,
          band_map: loadBandmap(this._storage(), this.stationId),
          listen_s: clamp(this.listenS, LISTEN_RANGE[0], LISTEN_RANGE[1], LISTEN_DEFAULT),
          hang_s: clamp(this.hangS, HANG_RANGE[0], HANG_RANGE[1], HANG_DEFAULT),
        });
      },

      control: function (action) {
        if (!ACTIONS[action] || !this.running || !this.canUse) return false;
        return this._send({ type: "scan_control", request_id: this._requestId(), action: action });
      },

      // -- incoming ---------------------------------------------------------------------
      _onScanState: function (msg) {
        if (!msg) return;
        if (msg.state === "stopped") {
          var wasRunning = this.scan !== null;
          this.scan = null;
          this._audioAnnounced = false;
          // The operator's own stop needs no explanation.
          if (wasRunning && msg.reason !== "operator") {
            this._notify({ ok: false, text: reasonText(msg.reason) });
          }
          return;
        }
        this.scan = msg;
        // One subscribe per scan: the carrier slots' RX audio, so a hit is audible.
        if (!this._audioAnnounced) {
          this._audioAnnounced = true;
          window.dispatchEvent(new CustomEvent("scan:audio", { detail: { slots: carrierSlots(this.slots) } }));
        }
      },

      _onReply: function (msg) {
        if (!msg || msg.request_id === undefined || !this._requests[msg.request_id]) return;
        delete this._requests[msg.request_id];
        this.pending = false;
        clearTimeout(this._pendingTimer);
        if (msg.type === "error") {
          this._notify({ ok: false, text: errorText((msg.error && msg.error.code) || msg.code) });
        }
      },

      _notify: function (notice) {
        var self = this;
        this.notice = notice;
        clearTimeout(this._noticeTimer);
        this._noticeTimer = setTimeout(function () {
          self.notice = null;
        }, NOTICE_MS);
      },
    };
  }

  var api = { logic: logic, scannerPanel: scannerPanel };
  if (typeof window !== "undefined") window.OE5XRXScannerPanel = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("alpine:init", function () {
      window.Alpine.data("scannerPanel", scannerPanel);
    });
  }
})();
