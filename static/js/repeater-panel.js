/* OE5XRX Repeater Panel — Alpine component (spec 2026-11-06 §5).
   Lists the repeaters this station can work (JSON from the control/repeaters/
   endpoint) and applies one with a single click: it sends
   {type:"apply_repeater", request_id, repeater_id, slot} through the control
   panel's socket via the window event "control:send"; the server resolves the
   values and relays one apply_preset to the agent.

   The control panel re-emits its frames as window events (control:state,
   control:result, control:error, control:inventory); this component only
   listens to them, it never owns a socket.

   Pure helpers are exported (window.OE5XRXRepeaterPanel / module.exports) for
   the Node test suite. Style follows control-panel.js (ES5-ish IIFE). */

(function () {
  "use strict";

  var STORAGE_PREFIX = "oe5xrx.bandmap.";
  var SEARCH_DEBOUNCE_MS = 250;
  var REFRESH_DEBOUNCE_MS = 600;
  var NOTICE_MS = 6000;
  // Last-resort unblock if no reply ever arrives (server preset timeout is 30 s).
  var PENDING_GIVEUP_MS = 35000;
  var TUNE_EPSILON = 0.0005; // MHz; module values round-trip as floats
  var BEARINGS = { N: 0, NE: 45, E: 90, SE: 135, S: 180, SW: 225, W: 270, NW: 315 };
  var RX_CAP = "rx_" + "frequency";
  var TX_CAP = "tx_" + "frequency";

  // -------------------------------------------------------------------------
  // Pure helpers
  // -------------------------------------------------------------------------
  function bandmapKey(stationId) {
    return STORAGE_PREFIX + String(stationId);
  }

  function loadBandmap(storage, stationId) {
    try {
      var raw = storage && storage.getItem(bandmapKey(stationId));
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

  function saveBandmap(storage, stationId, map) {
    try {
      if (storage) storage.setItem(bandmapKey(stationId), JSON.stringify(map || {}));
      return true;
    } catch (_) {
      return false; // private mode / quota: the choice just isn't remembered
    }
  }

  // Bands served by more than one module — only these get a dropdown.
  function multiBands(bands) {
    var out = [];
    for (var band in bands || {}) {
      if (Object.prototype.hasOwnProperty.call(bands, band) && (bands[band] || []).length > 1) {
        out.push(band);
      }
    }
    return out.sort();
  }

  // The slot a click on a ``band`` repeater goes to: the remembered choice if
  // that module still serves the band, else the band's first module.
  function resolveSlot(bands, bandmap, band) {
    var mods = (bands && bands[band]) || [];
    if (!mods.length) return null;
    var chosen = bandmap && bandmap[band];
    for (var i = 0; i < mods.length; i++) {
      if (mods[i].slot === chosen) return chosen;
    }
    return mods[0].slot;
  }

  // Module id serving ``band`` on ``slot`` (a slot may host several modules).
  function moduleOf(bands, band, slot) {
    var mods = (bands && bands[band]) || [];
    for (var i = 0; i < mods.length; i++) {
      if (mods[i].slot === slot) return mods[i].module;
    }
    return null;
  }

  function near(a, b) {
    var x = Number(a);
    var y = Number(b);
    return isFinite(x) && isFinite(y) && Math.abs(x - y) < TUNE_EPSILON;
  }

  // Slot currently tuned to ``row`` (rx = output, tx = input), else null.
  // ``tuning`` = {slot: {rx, tx}} from the module state.
  function tunedSlot(row, tuning) {
    for (var slot in tuning || {}) {
      if (!Object.prototype.hasOwnProperty.call(tuning, slot)) continue;
      var t = tuning[slot];
      if (t && near(t.rx, row.output_frequency) && near(t.tx, row.input_frequency)) return slot;
    }
    return null;
  }

  function formatMHz(value) {
    var n = Number(value);
    return isFinite(n) ? n.toFixed(4) : "";
  }

  // Repeater shift as the radio operator reads it: "−600 kHz", "+7.6 MHz", "simplex".
  function formatShift(row) {
    var d = Number(row.input_frequency) - Number(row.output_frequency);
    if (!isFinite(d)) return "";
    if (Math.abs(d) < TUNE_EPSILON) return "simplex";
    var sign = d > 0 ? "+" : "−";
    var abs = Math.abs(d);
    if (abs < 1) return sign + Math.round(abs * 1000) + " kHz";
    return sign + Number(abs.toFixed(3)) + " MHz";
  }

  function formatTone(tone) {
    if (!tone || tone === "none") return "";
    return /^\d{3}$/.test(tone) ? "D" + tone : tone;
  }

  function formatDistance(row) {
    if (row.distance_km === null || row.distance_km === undefined) return "";
    var km = Number(row.distance_km);
    var text = km < 10 ? km.toFixed(1) : String(Math.round(km));
    return text + " km" + (row.bearing ? " " + row.bearing : "");
  }

  function bearingDeg(label) {
    return Object.prototype.hasOwnProperty.call(BEARINGS, label) ? BEARINGS[label] : null;
  }

  function errorText(code) {
    switch (code) {
      case "not_locked":
        return "Take control of the station first.";
      case "forbidden":
        return "Your membership level may not change these settings.";
      case "not_applicable":
        return "This repeater is outside the module's bands, or the module went offline.";
      case "not_supported":
        return "Cross-band repeaters need two modules and are not supported yet.";
      case "ptt_active":
        return "Release PTT before switching repeaters.";
      case "not_connected":
        return "The station connection is down. Wait for it to reconnect.";
      case "timeout":
        return "The station did not answer in time. Check the module settings.";
      default:
        return "The repeater could not be applied.";
    }
  }

  // Human summary of an apply_preset result frame.
  function resultText(msg, callsign) {
    if (msg.ok === true) return { ok: true, text: "Tuned to " + callsign + "." };
    var code = msg.error && msg.error.code;
    if (code === "ptt_active") return { ok: false, text: errorText(code) };
    var failed = [];
    var detail = msg.value || {};
    for (var cap in detail) {
      if (Object.prototype.hasOwnProperty.call(detail, cap) && detail[cap] && !detail[cap].ok) {
        failed.push(cap.replace(/_/g, " "));
      }
    }
    if (failed.length) {
      return { ok: false, text: callsign + " applied partly. Not set: " + failed.join(", ") + "." };
    }
    return { ok: false, text: errorText(code) };
  }

  // {slot: {rx, tx}} from an inventory snapshot (the #control-initial shape).
  function tuningFromInventory(slots) {
    var out = {};
    if (!Array.isArray(slots)) return out;
    for (var i = 0; i < slots.length; i++) {
      var mods = (slots[i] && slots[i].modules) || [];
      for (var j = 0; j < mods.length; j++) {
        var st = mods[j] && mods[j].state;
        if (st && (RX_CAP in st || TX_CAP in st)) {
          out[String(slots[i].slot)] = { rx: st[RX_CAP], tx: st[TX_CAP] };
        }
      }
    }
    return out;
  }

  // Merge one state frame into ``tuning`` (returns the same object).
  function applyStateFrame(tuning, msg) {
    var values = (msg && msg.values) || {};
    if (!(RX_CAP in values) && !(TX_CAP in values)) return tuning;
    var key = String(msg.slot);
    var cur = tuning[key] || {};
    tuning[key] = {
      rx: RX_CAP in values ? values[RX_CAP] : cur.rx,
      tx: TX_CAP in values ? values[TX_CAP] : cur.tx,
    };
    return tuning;
  }

  var logic = {
    bandmapKey: bandmapKey,
    loadBandmap: loadBandmap,
    saveBandmap: saveBandmap,
    multiBands: multiBands,
    resolveSlot: resolveSlot,
    moduleOf: moduleOf,
    tunedSlot: tunedSlot,
    formatMHz: formatMHz,
    formatShift: formatShift,
    formatTone: formatTone,
    formatDistance: formatDistance,
    bearingDeg: bearingDeg,
    errorText: errorText,
    resultText: resultText,
    tuningFromInventory: tuningFromInventory,
    applyStateFrame: applyStateFrame,
  };

  // -------------------------------------------------------------------------
  // Alpine component
  // -------------------------------------------------------------------------
  function repeaterPanel() {
    return {
      stationId: null,
      url: null,
      q: "",
      loading: false,
      loadError: false,
      rows: [],
      bands: {},
      bandmap: {},
      hasCoords: true,
      attribution: null,
      tuning: {},
      pendingId: null, // repeater id of the in-flight apply
      notice: null, // {ok, text}

      _reqSeq: 0,
      _requests: {}, // request_id -> {id, callsign}
      _listeners: [],
      _searchTimer: null,
      _refreshTimer: null,
      _noticeTimer: null,
      _pendingTimer: null,
      _fetchSeq: 0,

      init: function () {
        var root = this.$el;
        this.stationId = root.getAttribute("data-station-id");
        this.url = root.getAttribute("data-url");
        this.bandmap = loadBandmap(this._storage(), this.stationId);
        this._seedTuning();
        var self = this;
        this._on("control:state", function (e) {
          applyStateFrame(self.tuning, e.detail);
        });
        this._on("control:result", function (e) {
          self._onReply(e.detail);
        });
        this._on("control:error", function (e) {
          self._onReply(e.detail);
        });
        // Modules came or went: the usable bands (and so the list) may change.
        this._on("control:inventory", function () {
          self._scheduleRefresh(REFRESH_DEBOUNCE_MS);
        });
        this.refresh();
      },

      destroy: function () {
        for (var i = 0; i < this._listeners.length; i++) {
          var l = this._listeners[i];
          window.removeEventListener(l.type, l.handler);
        }
        this._listeners = [];
        clearTimeout(this._searchTimer);
        clearTimeout(this._refreshTimer);
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

      _seedTuning: function () {
        var el = document.getElementById("control-initial");
        if (!el) return;
        try {
          this.tuning = tuningFromInventory(JSON.parse(el.textContent));
        } catch (_) {
          this.tuning = {};
        }
      },

      // -- data ---------------------------------------------------------------
      refresh: function () {
        if (!this.url) return Promise.resolve();
        var seq = ++this._fetchSeq;
        var self = this;
        var url = this.url + (this.q ? "?q=" + encodeURIComponent(this.q) : "");
        this.loading = true;
        return fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
          .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
          })
          .then(function (data) {
            if (seq !== self._fetchSeq) return; // a newer search superseded this one
            self.rows = Array.isArray(data.repeaters) ? data.repeaters : [];
            self.bands = data.bands || {};
            self.hasCoords = data.station_has_coords !== false;
            self.attribution = data.attribution || null;
            self.loadError = false;
          })
          .catch(function () {
            if (seq === self._fetchSeq) self.loadError = true;
          })
          .then(function () {
            if (seq === self._fetchSeq) self.loading = false;
          });
      },

      search: function () {
        var self = this;
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(function () {
          self.refresh();
        }, SEARCH_DEBOUNCE_MS);
      },

      _scheduleRefresh: function (ms) {
        var self = this;
        clearTimeout(this._refreshTimer);
        this._refreshTimer = setTimeout(function () {
          self.refresh();
        }, ms);
      },

      // -- band -> module mapping ----------------------------------------------
      get mappedBands() {
        return multiBands(this.bands);
      },

      modulesFor: function (band) {
        return this.bands[band] || [];
      },

      slotFor: function (band) {
        return resolveSlot(this.bands, this.bandmap, band);
      },

      chooseSlot: function (band, slot) {
        var next = {};
        for (var k in this.bandmap) {
          if (Object.prototype.hasOwnProperty.call(this.bandmap, k)) next[k] = this.bandmap[k];
        }
        next[band] = slot;
        this.bandmap = next;
        saveBandmap(this._storage(), this.stationId, next);
      },

      // -- rendering helpers ---------------------------------------------------
      rxMHz: function (row) {
        return formatMHz(row.output_frequency);
      },
      txMHz: function (row) {
        return formatMHz(row.input_frequency);
      },
      shift: function (row) {
        return formatShift(row);
      },
      tone: function (row) {
        return formatTone(row.input_tone);
      },
      distance: function (row) {
        return formatDistance(row);
      },
      arrowStyle: function (row) {
        var deg = bearingDeg(row.bearing);
        return deg === null ? { display: "none" } : { transform: "rotate(" + deg + "deg)" };
      },
      isTuned: function (row) {
        return tunedSlot(row, this.tuning) !== null;
      },
      isPending: function (row) {
        return this.pendingId === row.id;
      },

      get canApply() {
        var s = window.Alpine && window.Alpine.store && window.Alpine.store("control");
        return !!(s && s.connected && s.youHold && s.canControl !== false);
      },

      rowDisabled: function (row) {
        return row.cross_band || !this.canApply || this.pendingId !== null;
      },

      rowTitle: function (row) {
        if (row.cross_band) return "Cross-band – not yet supported";
        if (!this.canApply) return "Take control of the station to tune a repeater";
        return "Tune " + row.callsign;
      },

      // -- apply ----------------------------------------------------------------
      apply: function (row) {
        if (!row || this.rowDisabled(row)) return false;
        var slot = this.slotFor(row.rx_band);
        if (slot === null) return false;
        var rid = "rp-" + Date.now().toString(36) + "-" + ++this._reqSeq;
        this._requests[rid] = { id: row.id, callsign: row.callsign };
        this.pendingId = row.id;
        var self = this;
        clearTimeout(this._pendingTimer);
        this._pendingTimer = setTimeout(function () {
          self._onReply({ type: "error", request_id: rid, error: { code: "timeout" } });
        }, PENDING_GIVEUP_MS);
        window.dispatchEvent(
          new CustomEvent("control:send", {
            detail: {
              type: "apply_repeater",
              request_id: rid,
              repeater_id: row.id,
              slot: slot,
              module: moduleOf(this.bands, row.rx_band, slot),
            },
          }),
        );
        return true;
      },

      _onReply: function (msg) {
        if (!msg || msg.request_id === undefined) return;
        var req = this._requests[msg.request_id];
        if (!req) return; // not ours
        delete this._requests[msg.request_id];
        if (this.pendingId === req.id) {
          this.pendingId = null;
          clearTimeout(this._pendingTimer);
        }
        if (msg.type === "error") {
          var code = (msg.error && msg.error.code) || msg.code;
          this._notify({ ok: false, text: errorText(code) });
        } else {
          this._notify(resultText(msg, req.callsign));
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

  var api = { logic: logic, repeaterPanel: repeaterPanel };
  if (typeof window !== "undefined") window.OE5XRXRepeaterPanel = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("alpine:init", function () {
      window.Alpine.data("repeaterPanel", repeaterPanel);
    });
  }
})();
