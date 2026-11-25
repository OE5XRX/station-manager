/* OE5XRX SSTV section — Alpine component `sstvPanel` (SSTV spec §5.2–§5.4).
   Receive: start/stop the station's decoder, ISS button (a normal `command` that
   sets the RX frequency to 145.800 MHz), live picture on a canvas sized to the
   detected mode. Send: preview (POST, multipart) -> sstv_tx_start -> progress
   from sstv_tx_state, abort with sstv_tx_stop. Gallery: the sstv:gallery partial,
   reloaded via HTMX when a picture arrives.

   Like the scanner panel it owns no socket: frames go out via the window event
   "control:send", the server's frames come back as re-emitted control:* events.
   While a transmission is on the air it dispatches "sstv:tx" {active} so the
   control panel blocks voice PTT and the audio panel the microphone.

   Pure helpers live in sstv-logic.js (window.OE5XRXSstvLogic). */

(function () {
  "use strict";

  // i18n: globals from Django's JavaScriptCatalog; identity fallback under Node.
  // Function-scoped so classic <script> files never collide.
  var gettext = globalThis.gettext || function (s) { return s; };
  var interpolate = globalThis.interpolate || function (fmt, obj, named) {
    if (named) return fmt.replace(/%\(\s*([^)]+)\s*\)s/g, function (m, k) { return String(obj[k]); });
    return fmt.replace(/%s/g, function () { return String(obj.shift()); });
  };

  var S = (typeof window !== "undefined" && window.OE5XRXSstvLogic) || null;
  if (!S && typeof require === "function") S = require("./sstv-logic.js");

  var TX_EVENT = "sstv:tx";
  var GALLERY_EVENT = "sstv-gallery-refresh";
  var ISS_MHZ = 145.8;
  var NOTICE_MS = 9000;
  var PENDING_GIVEUP_MS = 15000;
  // A page that missed the end frame (socket gap) stops showing a session this long
  // after it must have ended: RX after ends_at, TX after its air time.
  var RX_STALE_MS = 30000;
  var TX_STALE_MS = 60000;
  var LIVE_TX = { sending: true, downloading: true, keyed: true };
  var LIVE_RX = { starting: true, running: true, paused: true };

  function readJson(id) {
    if (typeof document === "undefined" || !document.getElementById) return null;
    var el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (_) {
      return null;
    }
  }

  function sstvPanel() {
    // DOM handles stay out of Alpine's reactive proxy (a proxied canvas cannot draw)
    // and out of $refs (they live in the nested cpCollapsible scope).
    var rootEl = null;
    function part(name) {
      return rootEl && rootEl.querySelector ? rootEl.querySelector("[data-sv=" + name + "]") : null;
    }

    return {
      // config (data-* on the root)
      stationId: null,
      previewUrl: null,
      canUse: false, // sstv.use (server side); the lock is checked live
      hasStationCall: false,
      modes: [],

      // inventory
      slots: [],
      values: {}, // "slot module" -> {rx_frequency, tx_frequency} (MHz)
      selected: "", // "slot module" of the receiving module

      // receive
      rx: null, // {state, slot, endsAt} while a session is live
      picture: null, // {id, mode, width, height, row}
      lastImage: null, // {url, complete} of the latest finished picture

      // send
      source: "test",
      mode: "pd120",
      csUser: true,
      csStation: false,
      fileName: "",
      preview: null, // {id, url, duration_s, mode, slot, module, noCallsign}
      previewBusy: false,
      maxUploadBytes: 0,
      tx: null, // {id, state, progress, duration_s, mode, keyedAt, startedAt}

      pending: false,
      notice: null,
      now: Date.now(),

      _reqSeq: 0,
      _requests: {},
      _listeners: [],
      _noticeTimer: null,
      _pendingTimer: null,
      _tick: null,
      _txActive: false,
      _previewGen: 0, // bumped by every change of the choices: a late reply is dropped
      _imageGen: 0, // bumped by every new picture: a late PNG load never paints over it

      init: function () {
        var root = this.$el;
        rootEl = root;
        this.stationId = root.getAttribute("data-station-id");
        this.previewUrl = root.getAttribute("data-preview-url");
        this.maxUploadBytes = Number(root.getAttribute("data-max-upload-bytes")) || 0;
        this.canUse = root.getAttribute("data-can-use") === "1";
        this.hasStationCall = root.getAttribute("data-station-callsign") === "1";
        this.csStation = this.hasStationCall;
        this.modes = readJson("sstv-modes") || [];
        this._seed(readJson("control-initial") || []);
        var self = this;
        this._on("control:inventory", function (e) {
          self._seed((e.detail && e.detail.slots) || []);
        });
        this._on("control:state", function (e) {
          self._onModuleState(e.detail);
        });
        this._on("control:sstv_state", function (e) {
          self._onRxState(e.detail);
        });
        this._on("control:sstv_start", function (e) {
          self._onPictureStart(e.detail);
        });
        this._on("control:sstv_line", function (e) {
          self._onLine(e.detail);
        });
        this._on("control:sstv_image", function (e) {
          self._onImage(e.detail);
        });
        // (Re)connected: drop sessions whose end this page may have missed; the server's
        // snapshot right after restores a live RX and a transmission on the air.
        this._on("control:open", function () {
          self._settle();
          self._setRx(null);
          self._setTx(null);
        });
        this._on("control:sstv_tx_state", function (e) {
          self._onTxState(e.detail);
        });
        this._on("control:result", function (e) {
          self._onReply(e.detail);
        });
        this._on("control:error", function (e) {
          self._onReply(e.detail);
        });
      },

      destroy: function () {
        for (var i = 0; i < this._listeners.length; i++) {
          window.removeEventListener(this._listeners[i].type, this._listeners[i].handler);
        }
        this._listeners = [];
        clearTimeout(this._noticeTimer);
        clearTimeout(this._pendingTimer);
        clearInterval(this._tick);
        this._tick = null;
        this._announceTx(false);
      },

      _on: function (type, handler) {
        window.addEventListener(type, handler);
        this._listeners.push({ type: type, handler: handler });
      },

      // -- inventory --------------------------------------------------------------
      _seed: function (slots) {
        this.slots = Array.isArray(slots) ? slots : [];
        var values = {};
        for (var i = 0; i < this.slots.length; i++) {
          var mods = (this.slots[i] && this.slots[i].modules) || [];
          for (var j = 0; j < mods.length; j++) {
            var st = (mods[j] && mods[j].state) || {};
            var key = String(this.slots[i].slot) + " " + mods[j].module;
            var old = this.values[key] || {};
            // An inventory without a value (failed get, paused slot) keeps the last one.
            values[key] = {
              rx_frequency: st.rx_frequency !== undefined ? st.rx_frequency : old.rx_frequency,
              tx_frequency: st.tx_frequency !== undefined ? st.tx_frequency : old.tx_frequency,
            };
          }
        }
        this.values = values;
        var mods2 = this.rxModules;
        var keep = false;
        for (var k = 0; k < mods2.length; k++) {
          if (mods2[k].slot + " " + mods2[k].module === this.selected) keep = true;
        }
        if (!keep) this.selected = mods2.length ? mods2[0].slot + " " + mods2[0].module : "";
      },

      _onModuleState: function (msg) {
        if (!msg || msg.slot === undefined || typeof msg.module !== "string") return;
        var vals = msg.values || {};
        var key = String(msg.slot) + " " + msg.module;
        var cur = this.values[key] || {};
        var next = { rx_frequency: cur.rx_frequency, tx_frequency: cur.tx_frequency };
        if (Object.prototype.hasOwnProperty.call(vals, "rx_frequency")) next.rx_frequency = vals.rx_frequency;
        if (Object.prototype.hasOwnProperty.call(vals, "tx_frequency")) next.tx_frequency = vals.tx_frequency;
        var copy = Object.assign({}, this.values);
        copy[key] = next;
        this.values = copy;
      },

      get radios() {
        return S.radioModules(this.slots);
      },
      get rxModules() {
        return this.radios.filter(function (m) { return m.rx; });
      },
      get selectedModule() {
        var all = this.rxModules;
        for (var i = 0; i < all.length; i++) {
          if (all[i].slot + " " + all[i].module === this.selected) return all[i];
        }
        return null;
      },
      // The transmitting module: the preview's (exactly that one: the server sends with
      // it), else the selected one if it can send, else the first that can (the server
      // picks the same for a preview without one).
      get txModule() {
        var all = this.radios;
        var want = this.preview
          ? String(this.preview.slot) + " " + this.preview.module
          : this.selected;
        var first = null;
        for (var i = 0; i < all.length; i++) {
          if (!all[i].tx) continue;
          if (first === null) first = all[i];
          if (all[i].slot + " " + all[i].module === want) return all[i];
        }
        return this.preview ? null : first;
      },

      _hz: function (mod, cap) {
        if (!mod) return null;
        var v = this.values[mod.slot + " " + mod.module];
        return v ? S.mhzToHz(v[cap]) : null;
      },
      get rxHz() {
        var slot = this.rx ? String(this.rx.slot) : null;
        var mod = this.selectedModule;
        if (slot !== null) {
          var all = this.rxModules;
          for (var i = 0; i < all.length; i++) if (all[i].slot === slot) mod = all[i];
        }
        return this._hz(mod, "rx_frequency");
      },
      get txHz() {
        return this._hz(this.txModule, "tx_frequency");
      },
      rxMHz: function () {
        var hz = this.rxHz;
        return hz === null ? "–" : S.formatMHz(hz);
      },
      txMHz: function () {
        var hz = this.txHz;
        return hz === null ? "–" : S.formatMHz(hz);
      },
      get onIss() {
        return S.isIssFrequency(this.rxHz);
      },
      get txOnIss() {
        return S.isIssFrequency(this.txHz);
      },
      moduleLabel: function (m) {
        return interpolate(gettext("Slot %(slot)s (%(module)s)"), { slot: m.slot, module: m.module }, true);
      },

      // -- permissions -------------------------------------------------------------
      get holder() {
        var s = window.Alpine && window.Alpine.store && window.Alpine.store("control");
        return !!(s && s.connected && s.youHold && s.canControl !== false);
      },
      get canOperate() {
        return this.canUse && this.holder;
      },
      get rxLive() {
        return this.rx !== null;
      },
      get txLive() {
        return this.tx !== null && !!LIVE_TX[this.tx.state];
      },
      get canStartRx() {
        return this.canOperate && !this.pending && !!this.selectedModule && !this.txLive && !this.rxLive;
      },
      get canStopRx() {
        return this.canOperate && !this.pending && this.rxLive;
      },
      get canIss() {
        return this.canOperate && !this.pending && !!this.selectedModule && !this.txLive && !this.onIss;
      },
      get canPreview() {
        if (!this.canOperate || this.previewBusy || this.txLive || !this.txModule) return false;
        return this.source === "test" || !!this.fileName;
      },
      get canSend() {
        return this.canOperate && !this.pending && !this.previewBusy && !!this.preview && !this.txLive && !this.txOnIss;
      },
      get canAbort() {
        return this.canOperate && this.txLive;
      },
      // One line that says why "Send" is greyed out (only the first reason).
      sendHint: function () {
        if (!this.canUse) return "";
        if (!this.holder) return gettext("Take control of the station to send a picture.");
        if (this.txLive) return "";
        if (this.txOnIss) return gettext("The TX frequency is the ISS downlink (145.800 MHz). Sending is blocked there.");
        if (!this.preview) return gettext("Create a preview first. It shows exactly what will be sent.");
        return "";
      },

      // -- status texts ------------------------------------------------------------------
      rxStateLabel: function () {
        if (!this.rx) return gettext("Not receiving");
        if (this.rx.state === "starting") return gettext("Starting receiver");
        if (this.rx.state === "paused") return gettext("Paused while sending");
        return this.picture ? gettext("Receiving a picture") : gettext("Listening for a picture");
      },
      remaining: function () {
        return this.rx ? S.formatRemaining(this.rx.endsAt, this.now) : "";
      },
      pictureLabel: function () {
        var p = this.picture;
        if (!p) return "";
        var rows = Math.min(p.height, p.row + 1);
        return interpolate(gettext("%(mode)s, row %(row)s of %(rows)s"),
          { mode: this.modeName(p.mode), row: rows, rows: p.height }, true);
      },
      modeName: function (key) {
        for (var i = 0; i < this.modes.length; i++) if (this.modes[i].key === key) return this.modes[i].name;
        return key || "";
      },
      get modeInfo() {
        for (var i = 0; i < this.modes.length; i++) if (this.modes[i].key === this.mode) return this.modes[i];
        return null;
      },
      // Header chip of the (possibly collapsed) section.
      summary: function () {
        if (this.txLive) {
          return this.tx.state === "keyed"
            ? interpolate(gettext("On the air, %(pct)s"), { pct: this.txPercent() + " %" }, true)
            : S.txPhaseText(this.tx.state);
        }
        if (this.rx) {
          var left = this.remaining();
          return left ? this.rxStateLabel() + " " + left : this.rxStateLabel();
        }
        return "";
      },
      txPhase: function () {
        return this.tx ? S.txPhaseText(this.tx.state) : "";
      },
      txPercent: function () {
        if (!this.tx) return 0;
        var p = this.tx.progress || 0;
        if (this.tx.state === "keyed" && this.tx.keyedAt && this.tx.duration_s > 0) {
          p = Math.max(p, (this.now - this.tx.keyedAt) / 1000 / this.tx.duration_s);
        }
        return Math.round(Math.min(1, Math.max(0, p)) * 100);
      },
      txLeft: function () {
        if (!this.tx || this.tx.state !== "keyed" || !this.tx.duration_s) return "";
        return S.formatSeconds(this.tx.duration_s * (1 - this.txPercent() / 100));
      },
      // Preview wipe: the part of the picture already sent.
      sentStyle: function () {
        return "height:" + (this.txLive ? this.txPercent() : 0) + "%";
      },
      durationText: function (seconds) {
        return S.formatSeconds(seconds);
      },

      // -- clock ---------------------------------------------------------------------------
      _syncClock: function () {
        var need = this.rx !== null || this.txLive;
        var self = this;
        if (need && this._tick === null) {
          this.now = Date.now();
          this._tick = setInterval(function () {
            self.now = Date.now();
            self._expireStale();
          }, 1000);
        } else if (!need && this._tick !== null) {
          clearInterval(this._tick);
          this._tick = null;
        }
      },

      _expireStale: function () {
        if (this.rx && this.rx.endsAt) {
          var end = Date.parse(this.rx.endsAt);
          if (isFinite(end) && this.now > end + RX_STALE_MS) this._setRx(null);
        }
        if (this.txLive && this.tx.startedAt) {
          var limit = (this.tx.duration_s || 0) * 1000 + TX_STALE_MS + 120000;
          if (this.now - this.tx.startedAt > limit) this._setTx(null);
        }
      },

      // -- frames out ---------------------------------------------------------------------
      _requestId: function () {
        return "sv-" + Date.now().toString(36) + "-" + ++this._reqSeq;
      },

      _send: function (frame) {
        var self = this;
        this.pending = true;
        this._requests[frame.request_id] = frame.type;
        clearTimeout(this._pendingTimer);
        this._pendingTimer = setTimeout(function () {
          self._onReply({ type: "error", request_id: frame.request_id, error: { code: "timeout" } });
        }, PENDING_GIVEUP_MS);
        window.dispatchEvent(new CustomEvent("control:send", { detail: frame }));
        return true;
      },

      startRx: function () {
        var m = this.selectedModule;
        if (!this.canStartRx || !m) return false;
        return this._send({ type: "sstv_rx_start", request_id: this._requestId(), slot: m.slot });
      },

      stopRx: function () {
        if (!this.canStopRx) return false;
        return this._send({ type: "sstv_rx_stop", request_id: this._requestId() });
      },

      // ISS: a normal module command on the receiving module (RX = 145.800 MHz).
      tuneIss: function () {
        var m = this.selectedModule;
        if (!this.canIss || !m) return false;
        var store = window.Alpine && window.Alpine.store && window.Alpine.store("control");
        var rid = this._requestId();
        var sent = !!(store && store.sendCommand({
          type: "command",
          request_id: rid,
          slot: /^\d+$/.test(m.slot) ? Number(m.slot) : m.slot,
          module: m.module,
          capability: "rx_frequency",
          op: "set",
          value: ISS_MHZ,
        }));
        if (!sent) {
          this._notify({ ok: false, text: S.sendGuardMessage("not_connected") });
          return false;
        }
        this._requests[rid] = "command";
        return true;
      },

      send: function (confirmed) {
        if (!this.canSend) return false;
        if (this.preview.noCallsign && confirmed !== true) {
          var dlg = part("confirm");
          if (dlg && typeof dlg.showModal === "function") dlg.showModal();
          return false;
        }
        var frame = {
          type: "sstv_tx_start",
          request_id: this._requestId(),
          transmission_id: this.preview.id,
        };
        if (confirmed === true) frame.confirm_no_callsign = true;
        return this._send(frame);
      },

      confirmSend: function () {
        var dlg = part("confirm");
        if (dlg && typeof dlg.close === "function") dlg.close();
        return this.send(true);
      },

      cancelSend: function () {
        var dlg = part("confirm");
        if (dlg && typeof dlg.close === "function") dlg.close();
      },

      abort: function () {
        if (!this.canAbort) return false;
        return this._send({ type: "sstv_tx_stop", request_id: this._requestId() });
      },

      // -- preview (HTTP) -------------------------------------------------------------------
      onFile: function (event) {
        var f = event && event.target && event.target.files && event.target.files[0];
        this.fileName = f ? f.name : "";
        this.invalidate();
      },

      // Any change of what would be sent invalidates the preview: "Send" sends exactly it.
      invalidate: function () {
        this.preview = null;
        this._previewGen += 1;
      },

      _csrf: function () {
        var el = rootEl && rootEl.querySelector ? rootEl.querySelector("input[name=csrfmiddlewaretoken]") : null;
        return el ? el.value : "";
      },

      makePreview: function () {
        if (!this.canPreview) return Promise.resolve(false);
        var form = new FormData();
        form.append("source", this.source);
        form.append("mode", this.mode);
        form.append("cs_user", this.csUser ? "1" : "0");
        form.append("cs_station", this.csStation && this.hasStationCall ? "1" : "0");
        var m = this.txModule;
        if (m) {
          form.append("slot", m.slot);
          form.append("module", m.module);
        }
        if (this.source === "upload") {
          var input = part("file");
          var f = input && input.files && input.files[0];
          if (!f) {
            this._notify({ ok: false, text: S.sendGuardMessage("no_file") });
            return Promise.resolve(false);
          }
          // Same limits as the server (render.load_upload), checked before the upload.
          var refusal = S.uploadRefusal(f, this.maxUploadBytes);
          if (refusal) {
            this._notify({ ok: false, text: refusal });
            return Promise.resolve(false);
          }
          form.append("file", f);
        }
        var self = this;
        this.invalidate();
        var gen = this._previewGen;
        this.previewBusy = true;
        return fetch(this.previewUrl, {
          method: "POST",
          body: form,
          credentials: "same-origin",
          headers: { "X-CSRFToken": this._csrf(), Accept: "application/json" },
        })
          .then(function (r) {
            return r.json().then(
              function (body) { return { ok: r.ok, body: body || {} }; },
              function () { return { ok: false, body: {} }; }
            );
          })
          .then(function (res) {
            if (gen !== self._previewGen) return false; // the choices changed meanwhile
            if (!res.ok) {
              // The server explains its refusals (translated); upload checks only it knows.
              var text = typeof res.body.message === "string" && res.body.message
                ? res.body.message
                : S.sendGuardMessage(res.body.error || "internal_error");
              self._notify({ ok: false, text: text });
              return false;
            }
            var b = res.body;
            var warnings = Array.isArray(b.warnings) ? b.warnings : [];
            self.preview = {
              id: b.id,
              url: b.preview_url,
              duration_s: b.duration_s,
              mode: b.mode,
              slot: b.slot,
              module: b.module,
              noCallsign: warnings.indexOf("no_callsign") >= 0,
            };
            return true;
          })
          .catch(function () {
            if (gen !== self._previewGen) return false;
            self._notify({ ok: false, text: gettext("The preview could not be created. Check the connection and try again.") });
            return false;
          })
          .then(function (ok) {
            self.previewBusy = false;
            return ok;
          });
      },

      // -- frames in --------------------------------------------------------------------------
      _settle: function () {
        var kept = {};
        for (var rid in this._requests) {
          if (this._requests[rid] === "command") kept[rid] = "command";
        }
        this._requests = kept;
        this.pending = false;
        clearTimeout(this._pendingTimer);
      },

      _setRx: function (rx) {
        this.rx = rx;
        this._syncClock();
      },

      _onRxState: function (msg) {
        if (!msg) return;
        this._settle();
        if (LIVE_RX[msg.state]) {
          if (!this.rx) this.picture = null;
          this._setRx({ state: msg.state, slot: msg.slot, endsAt: msg.ends_at || null });
          return;
        }
        var wasLive = this.rx !== null;
        this._setRx(null);
        var text = S.rxEndText(msg.state, msg.detail);
        if (text && (wasLive || msg.state === "error")) this._notify({ ok: false, text: text });
      },

      _canvas: function () {
        return part("canvas");
      },

      _onPictureStart: function (msg) {
        if (!msg || typeof msg.picture_id !== "string") return;
        var w = Number(msg.width);
        var h = Number(msg.height);
        if (!(w > 0 && w <= 2048 && h > 0 && h <= 2048)) return;
        this._imageGen += 1;
        this.picture = { id: msg.picture_id, mode: msg.mode, width: w, height: h, row: -1 };
        var c = this._canvas();
        if (c) {
          c.width = w;
          c.height = h;
          var ctx = c.getContext && c.getContext("2d");
          if (ctx) ctx.clearRect(0, 0, w, h);
        }
      },

      _onLine: function (msg) {
        var p = this.picture;
        if (!p || !msg || msg.picture_id !== p.id) return;
        var row = msg.row;
        if (typeof row !== "number" || row < 0 || row >= p.height) return;
        var rgba = S.decodeRow(msg.rgb);
        var px = Math.min(p.width, rgba.length / 4);
        if (px < 1) return;
        var c = this._canvas();
        var ctx = c && c.getContext && c.getContext("2d");
        if (ctx && typeof ImageData === "function") {
          ctx.putImageData(new ImageData(rgba.slice(0, px * 4), px, 1), 0, row);
        }
        if (row > p.row) p.row = row;
      },

      // The authoritative picture: show it in the monitor and refresh the gallery.
      _onImage: function (msg) {
        if (!msg || typeof msg.url !== "string") return;
        // The monitor shows received pictures only; a sent one just refreshes the gallery.
        if (msg.direction !== "tx") {
          this.lastImage = { url: msg.url, complete: msg.complete !== false };
          this.picture = null;
          var gen = ++this._imageGen;
          var self = this;
          var c = this._canvas();
          if (c && typeof Image === "function") {
            var img = new Image();
            img.onload = function () {
              if (gen !== self._imageGen || self.picture) return; // a newer picture owns the canvas
              c.width = img.naturalWidth;
              c.height = img.naturalHeight;
              var ctx = c.getContext && c.getContext("2d");
              if (ctx) ctx.drawImage(img, 0, 0);
            };
            img.src = msg.url;
          }
        }
        if (typeof document !== "undefined" && document.body) {
          document.body.dispatchEvent(new CustomEvent(GALLERY_EVENT));
        }
      },

      _setTx: function (tx) {
        this.tx = tx;
        this._announceTx(this.txLive);
        this._syncClock();
      },

      _announceTx: function (active) {
        if (active === this._txActive) return;
        this._txActive = active;
        window.dispatchEvent(new CustomEvent(TX_EVENT, { detail: { active: active } }));
      },

      _onTxState: function (msg) {
        if (!msg || typeof msg.state !== "string") return;
        this._settle();
        var prev = this.tx && this.tx.id === msg.id ? this.tx : null;
        if (LIVE_TX[msg.state]) {
          this._setTx({
            id: msg.id,
            state: msg.state,
            progress: Number(msg.progress) || 0,
            duration_s: Number(msg.duration_s) || 0,
            mode: msg.mode,
            startedAt: prev ? prev.startedAt : Date.now(),
            keyedAt: prev && prev.keyedAt ? prev.keyedAt : msg.state === "keyed" ? Date.now() : null,
          });
          return;
        }
        // Only the sender's page holds the preview of this transmission.
        var mine = !!(this.preview && this.preview.id === msg.id);
        var seen = prev !== null || mine;
        this._setTx(null);
        // Sent, aborted or failed: the draft is used up, a resend needs a new preview.
        if (mine) this.invalidate();
        if (mine || (seen && msg.state !== "done")) {
          this._notify({ ok: msg.state === "done", text: S.txEndText(msg.state, msg.detail, mine) });
        }
      },

      _onReply: function (msg) {
        if (!msg || msg.request_id === undefined || !this._requests[msg.request_id]) return;
        var kind = this._requests[msg.request_id];
        delete this._requests[msg.request_id];
        if (kind !== "command") {
          this.pending = false;
          clearTimeout(this._pendingTimer);
        }
        var failed = msg.type === "error" || msg.ok === false;
        if (!failed) return;
        var code = (msg.error && msg.error.code) || msg.code;
        if (code === "no_active_sstv") {
          // Ended without this page hearing it: show it as ended.
          if (kind === "sstv_rx_stop") this._setRx(null);
          if (kind === "sstv_tx_stop") this._setTx(null);
          return;
        }
        if (code === "no_callsign" && this.preview) {
          this.preview.noCallsign = true;
          this.send(false);
          return;
        }
        if (code === "not_found" || code === "expired") this.preview = null;
        if (kind === "command") {
          this._notify({ ok: false, text: gettext("The ISS frequency could not be set.") });
          return;
        }
        this._notify({ ok: false, text: S.sendGuardMessage(code) });
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

  var api = { sstvPanel: sstvPanel };
  if (typeof window !== "undefined") window.OE5XRXSstvPanel = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("alpine:init", function () {
      window.Alpine.data("sstvPanel", sstvPanel);
    });
  }
})();
