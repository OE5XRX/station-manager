/* OE5XRX Control Panel — live Alpine component.
   Owns the D4 control WebSocket, all reactive state, and every method the
   widget templates call: valueOf, displayValue, setValue, stepValue, doAction,
   isPending, errorOf, moduleOnline, canControl, telemetry, ptt, lock methods.

   Pure logic lives in control-logic.js (window.OE5XRXControlLogic); this file
   is the stateful shell: connection, message routing, PTT state machine wiring,
   lock UX, and the fail-safe unkey paths.

   CSP: loaded via <script src> with a nonce; no inline logic. Style follows
   static/js/app.js (ES5-ish, "use strict"); the Alpine object uses modern
   method syntax as the component contract requires. */

(function () {
  "use strict";

  var L = window.OE5XRXControlLogic;
  var PTT_CAP = "ptt"; // the only capability name referenced literally (D3 §8)
  var KEEPALIVE_MS = 1000;
  var PTT_KEY_STORAGE = "oe5xrx.ptt_key";
  var LOSS_NOTICE_MS = 4000;

  function controlPanel() {
    return {
      // -- reactive state ----------------------------------------------------
      conn: "connecting", // 'connecting' | 'open' | 'closed'
      agentOffline: false,
      connError: null, // terminal connection-error reason (4401/4403/4404 deny)
      values: {}, // "slot module cap" -> setting value
      telemetry: {}, // "slot module cap" -> telemetry value
      online: {}, // "slot module" -> bool
      pending: {}, // "slot module cap" -> true
      errors: {}, // "slot module cap" (or "slot module") -> human string
      ptt: {}, // "slot module" -> 'armed' | 'keying' | 'tx'
      // lock
      lockState: "free", // 'free' | 'held' | 'other'
      lockHolder: null,
      youHold: false,
      pendingRequest: null, // {id, username}
      lossNotice: false,
      // config (from data-* on root)
      stationId: null,
      canAdmin: false,
      pttKey: " ",

      // -- non-reactive internals (underscore-prefixed) ----------------------
      _ws: null,
      _retry: 0,
      _closeCode: null, // last WS close code; inspected in _onDisconnect
      _caps: {}, // "slot module cap" -> descriptor
      _capNames: {}, // "slot module" -> {settings:[], telemetry:[]}
      _reqWidget: {}, // request_id -> "slot module cap"
      _reqSeq: 0,
      _listeners: [],
      _lossTimer: null,
      _closed: false,

      // ---------------------------------------------------------------------
      // init
      // ---------------------------------------------------------------------
      init: function () {
        var root = this.$el;
        this.stationId = root.getAttribute("data-station-id");
        var store = window.Alpine.store && window.Alpine.store("control");
        if (store) {
          store.stationId = this.stationId;
          store._send = this._send.bind(this);
        }
        this.canAdmin = root.getAttribute("data-can-admin") === "1";
        var defaultKey = root.getAttribute("data-ptt-default-key");
        if (defaultKey === null || defaultKey === "") defaultKey = " ";
        var stored = null;
        try {
          stored = localStorage.getItem(PTT_KEY_STORAGE);
        } catch (_) {
          stored = null;
        }
        this.pttKey = stored !== null && stored !== "" ? stored : defaultKey;

        this._seedFromInitial();
        this._installListeners();
        this._connect();
      },

      destroy: function () {
        this._teardown();
      },

      _publishControlStore: function () {
        var s = window.Alpine.store && window.Alpine.store("control");
        if (!s) return;
        s.youHold = this.youHold;
        s.keyed = this._anyKeyed();
        s.canControl = this.canControl;
        s.connected = this.conn === "open" && !this.agentOffline;
      },

      // ---------------------------------------------------------------------
      // Seed from the server-rendered #control-initial inventory snapshot.
      // First paint is correct AND offline-safe (last_state renders even if the
      // agent is down).
      // ---------------------------------------------------------------------
      _seedFromInitial: function () {
        var el = document.getElementById("control-initial");
        if (!el) return;
        var slots;
        try {
          slots = JSON.parse(el.textContent);
        } catch (_) {
          return;
        }
        this._ingestInventory(slots);
      },

      _ingestInventory: function (slots) {
        if (!Array.isArray(slots)) return;
        // C1: A live inventory frame is proof the agent is (re)connected.
        // Clear the agentOffline latch so canControl/moduleOnline unfreeze
        // when the agent reconnects while the browser socket stays open (no
        // browser "open" fires in that case, so this is the only reliable path).
        this.agentOffline = false;
        // Rebuild the descriptor/topology maps from scratch: a refreshed
        // inventory that REMOVES a module/capability (topology change or a
        // reconnect with different modules) must not leave stale entries, which
        // would otherwise mislead keyboard PTT, _subscribeAll, and cached bounds.
        // Live value/telemetry/ptt state is keyed and preserved for surviving
        // modules; entries for removed modules go inert once they drop out of
        // _capNames.
        this._caps = {};
        this._capNames = {};
        this.online = {};
        this._boundsCache = {};
        for (var i = 0; i < slots.length; i++) {
          var slot = slots[i].slot;
          var mods = slots[i].modules || [];
          for (var j = 0; j < mods.length; j++) {
            var m = mods[j];
            var mkey = L.moduleKey(slot, m.module);
            this.online[mkey] = m.online !== false;
            var caps = m.capabilities || [];
            var settingNames = [];
            var telemNames = [];
            for (var c = 0; c < caps.length; c++) {
              var cap = caps[c];
              this._caps[L.widgetKey(slot, m.module, cap.name)] = cap;
              if (cap.kind === "telemetry") telemNames.push(cap.name);
              else if (cap.kind === "setting") settingNames.push(cap.name);
            }
            this._capNames[mkey] = { settings: settingNames, telemetry: telemNames };
            // Seed values/telemetry from persisted state, without clobbering a
            // pending edit that has not yet been confirmed.
            var state = m.state || {};
            for (var name in state) {
              if (!Object.prototype.hasOwnProperty.call(state, name)) continue;
              var wkey = L.widgetKey(slot, m.module, name);
              var descr = this._caps[wkey];
              if (descr && descr.kind === "telemetry") {
                this.telemetry[wkey] = state[name];
              } else if (!this.pending[wkey]) {
                this.values[wkey] = state[name];
              }
            }
            if (this.ptt[mkey] === undefined) this.ptt[mkey] = "armed";
          }
        }
        this._publishControlStore();
      },

      // ---------------------------------------------------------------------
      // WebSocket
      // ---------------------------------------------------------------------
      _wsUrl: function () {
        var proto = location.protocol === "https:" ? "wss:" : "ws:";
        return proto + "//" + location.host + "/ws/control/" + this.stationId + "/";
      },

      _connect: function () {
        if (this._closed) return;
        this.conn = "connecting";
        var self = this;
        var ws;
        try {
          ws = new WebSocket(this._wsUrl());
        } catch (_) {
          this._scheduleReconnect();
          return;
        }
        this._ws = ws;
        ws.addEventListener("open", function () {
          self._retry = 0;
          self.conn = "open";
          self.agentOffline = false;
          self._subscribeAll();
          self._publishControlStore();
        });
        ws.addEventListener("message", function (ev) {
          var msg;
          try {
            msg = JSON.parse(ev.data);
          } catch (_) {
            return;
          }
          self._route(msg);
        });
        ws.addEventListener("close", function (ev) {
          self._closeCode = ev.code || null;
          self._onDisconnect();
          self._scheduleReconnect();
          self._publishControlStore();
        });
        ws.addEventListener("error", function () {
          try {
            ws.close();
          } catch (_) {}
        });
      },

      _onDisconnect: function () {
        this.conn = "closed";
        // Fail-safe: a dropped socket must unkey every module locally.
        this._unkeyAll(false);
      },

      _scheduleReconnect: function () {
        if (this._closed) return;
        // Permanent-deny codes: do not retry — set terminal state instead.
        var code = this._closeCode;
        if (code === 4401 || code === 4403 || code === 4404) {
          this.conn = "closed";
          // The server normally sends a {type:"error", reason:...} frame first,
          // which sets connError. If that frame was missed (network drop / parse
          // failure) connError stays null and the pill would wrongly read
          // "Reconnecting" even though we've stopped retrying — derive a fallback
          // from the close code so the banner stays accurate.
          if (!this.connError) {
            this.connError =
              code === 4401
                ? "You are not signed in."
                : code === 4403
                  ? "You are not permitted to control this station."
                  : "Station not found.";
          }
          return;
        }
        this._retry += 1;
        var wait = Math.min(30000, 1000 * Math.pow(1.6, this._retry));
        var self = this;
        setTimeout(function () {
          self._connect();
        }, wait);
      },

      _send: function (obj) {
        if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return false;
        try {
          // Stamp the §7 envelope version (L.PROTOCOL_VERSION) — the agent's
          // parse_message drops any frame whose "v" mismatches, so command/
          // subscribe/ptt_keepalive MUST carry it.
          this._ws.send(JSON.stringify(L.envelope(obj)));
          return true;
        } catch (_) {
          return false;
        }
      },

      _isOpen: function () {
        return !!this._ws && this._ws.readyState === WebSocket.OPEN;
      },

      // ---------------------------------------------------------------------
      // Message router
      // ---------------------------------------------------------------------
      _route: function (msg) {
        switch (msg.type) {
          case "inventory":
            this._ingestInventory(msg.slots || []);
            // Re-subscribe telemetry: a fresh inventory frame means the agent
            // (re)connected or the topology changed while our socket stayed
            // open, and no "open" event fires in that case — without this,
            // live telemetry would silently stop after an agent reconnect.
            this._subscribeAll();
            break;
          case "state":
            this._onState(msg);
            break;
          case "result":
            this._onResult(msg);
            break;
          case "error":
            this._onError(msg);
            break;
          case "event":
            this._onEvent(msg);
            break;
          case "lock":
            this._onLock(msg);
            break;
          case "agent_offline":
            this.agentOffline = true;
            this._unkeyAll(true);
            break;
          case "control_requested":
            if (msg.requester) {
              this.pendingRequest = {
                id: msg.requester.id,
                username: msg.requester.username,
              };
            }
            break;
          default:
            break; // forward-compat: ignore unknown types
        }
      },

      _onState: function (msg) {
        var slot = msg.slot;
        var module = msg.module;
        var values = msg.values || {};
        var mkey = L.moduleKey(slot, module);
        for (var name in values) {
          if (!Object.prototype.hasOwnProperty.call(values, name)) continue;
          var wkey = L.widgetKey(slot, module, name);
          var descr = this._caps[wkey];
          if (descr && descr.kind === "telemetry") {
            this.telemetry[wkey] = values[name];
          } else {
            // Authoritative server value wins over any local pending edit.
            this.values[wkey] = values[name];
          }
          if (this.pending[wkey]) delete this.pending[wkey];
        }
        // PTT confirmed-TX transition, driven by the module's own ptt state.
        if (Object.prototype.hasOwnProperty.call(values, PTT_CAP)) {
          if (values[PTT_CAP] === true) {
            this.ptt[mkey] = L.nextPttPhase(this.ptt[mkey] || "armed", "confirm");
          } else if (values[PTT_CAP] === false && L.isKeyed(this.ptt[mkey])) {
            this._unkeyModule(mkey, false);
          }
        }
        this._publishControlStore();
      },

      _onResult: function (msg) {
        var rid = msg.request_id;
        var wkey = this._reqWidget[rid];
        if (rid !== undefined) delete this._reqWidget[rid];
        if (wkey && this.pending[wkey]) delete this.pending[wkey];
        var err = msg.error || (msg.ok === false ? { code: "bad_value" } : null);
        if (err && wkey) {
          this.errors[wkey] = L.errorMessage(err.code);
        } else if (wkey) {
          delete this.errors[wkey];
        }
      },

      _onError: function (msg) {
        var rid = msg.request_id;
        var wkey = rid !== undefined ? this._reqWidget[rid] : null;
        if (rid !== undefined) delete this._reqWidget[rid];
        if (wkey && this.pending[wkey]) delete this.pending[wkey];
        var code = msg.error && msg.error.code ? msg.error.code : msg.code;
        if (wkey) {
          this.errors[wkey] = L.errorMessage(code);
        } else if (msg.reason) {
          // Connect-time rejection frame (4401/4403/4404): no request_id, but
          // msg.reason contains a human-readable explanation. Surface it as a
          // terminal connection error in the banner area so the user knows why
          // they are not able to connect (not just an endless Reconnecting pill).
          this.connError = msg.reason;
        }
      },

      _onEvent: function (msg) {
        var slot = msg.slot;
        var module = msg.module;
        var mkey = L.moduleKey(slot, module);
        switch (msg.event) {
          case "ptt_auto_unkey":
            // Belt-and-suspenders: the agent already unkeyed (dead-man), but we
            // still send ptt=false when the socket is open so browser and agent
            // never disagree about carrier state (spec §5 fail-safe list).
            this._unkeyModule(mkey, true);
            break;
          case "module_added":
            this.online[mkey] = true;
            break;
          case "module_removed":
            this.online[mkey] = false;
            this._unkeyModule(mkey, false);
            break;
          case "module_error":
            this.errors[mkey] =
              (msg.detail && msg.detail.msg) || L.errorMessage("bad_value");
            break;
          default:
            break;
        }
      },

      _onLock: function (msg) {
        var wasKeyed = this._anyKeyed();
        var prevYouHold = this.youHold;
        this.youHold = !!msg.you_hold;
        this.lockState = L.deriveLockState(msg.state, this.youHold);
        this.lockHolder = msg.holder_username || null;
        // Lock-loss while operating: if we were keyed and no longer hold,
        // force unkey everywhere and surface a transient notice.
        if (prevYouHold && !this.youHold && wasKeyed) {
          this._unkeyAll(true);
          this._showLossNotice();
        } else if (prevYouHold && !this.youHold) {
          this._unkeyAll(true);
        }
        // A stale request prompt for a lock we no longer hold is meaningless.
        if (!this.youHold) this.pendingRequest = null;
        this._publishControlStore();
      },

      _showLossNotice: function () {
        var self = this;
        this.lossNotice = true;
        if (this._lossTimer) clearTimeout(this._lossTimer);
        this._lossTimer = setTimeout(function () {
          self.lossNotice = false;
        }, LOSS_NOTICE_MS);
      },

      // ---------------------------------------------------------------------
      // Telemetry subscription
      // ---------------------------------------------------------------------
      _subscribeAll: function () {
        this._forEachModule(function (slot, module, names, caps) {
          if (!names.telemetry.length) return;
          var descrs = [];
          for (var i = 0; i < names.telemetry.length; i++) {
            descrs.push(caps[L.widgetKey(slot, module, names.telemetry[i])] || {});
          }
          this._send({
            type: "subscribe",
            slot: L.slotAddr(slot),
            module: module,
            capabilities: names.telemetry.slice(),
            interval_ms: L.subscribeInterval(descrs),
          });
        });
      },

      _unsubscribeAll: function () {
        this._forEachModule(function (slot, module, names) {
          if (!names.telemetry.length) return;
          this._send({
            type: "unsubscribe",
            slot: L.slotAddr(slot),
            module: module,
            capabilities: names.telemetry.slice(),
          });
        });
      },

      _forEachModule: function (fn) {
        for (var mkey in this._capNames) {
          if (!Object.prototype.hasOwnProperty.call(this._capNames, mkey)) continue;
          var parts = mkey.split(" ");
          var slot = parts[0];
          var module = parts.slice(1).join(" ");
          fn.call(this, slot, module, this._capNames[mkey], this._caps);
        }
      },

      // ---------------------------------------------------------------------
      // Getters the templates read
      // ---------------------------------------------------------------------
      get canControl() {
        return this.youHold && this.conn === "open" && !this.agentOffline;
      },

      moduleOnline: function (slot, module) {
        if (this.agentOffline) return false;
        var v = this.online[L.moduleKey(slot, module)];
        return v !== false && v !== undefined;
      },

      // A widget is operable only if the user controls AND the module is online
      // — commands to an offline module would just time out. Templates gate
      // per-module widgets on this (not bare canControl).
      canOperate: function (slot, module) {
        return this.canControl && this.moduleOnline(slot, module);
      },

      valueOf: function (slot, module, cap) {
        return this.values[L.widgetKey(slot, module, cap)];
      },

      displayValue: function (slot, module, cap) {
        var wkey = L.widgetKey(slot, module, cap);
        var v = this.values[wkey];
        if (v === undefined || v === null) return "";
        var descr = this._caps[wkey];
        if (descr && (descr.type === "float" || descr.type === "int")) {
          var n = L.parseNumber(v);
          return n === null ? String(v) : L.formatNumber(n);
        }
        return v;
      },

      isPending: function (slot, module, cap) {
        return !!this.pending[L.widgetKey(slot, module, cap)];
      },

      errorOf: function (slot, module, cap) {
        return this.errors[L.widgetKey(slot, module, cap)] || "";
      },

      telemetryText: function (slot, module, cap) {
        var wkey = L.widgetKey(slot, module, cap);
        var v = this.telemetry[wkey];
        if (v === undefined || v === null) return "—";
        var descr = this._caps[wkey];
        if (descr && (descr.type === "float" || descr.type === "int")) {
          var n = L.parseNumber(v);
          return n === null ? String(v) : L.formatNumber(n);
        }
        return v;
      },

      telemetryPct: function (slot, module, cap) {
        var wkey = L.widgetKey(slot, module, cap);
        var v = this.telemetry[wkey];
        var bounds = this._bounds(slot, module, cap);
        return L.telemetryPercent(v, bounds.min, bounds.max);
      },

      _bounds: function (slot, module, cap) {
        // Bounds are static per widget (data-* attrs are rendered from the
        // descriptor and never change), but telemetryPct() is called twice per
        // meter on every telemetry tick — cache the result so the DOM query
        // happens at most once per widget key, not on every frame.
        var key = L.widgetKey(slot, module, cap);
        var cache = this._boundsCache || (this._boundsCache = {});
        if (cache[key]) return cache[key];
        // Prefer the live DOM data-min/data-max (authoritative for the widget),
        // fall back to the cap descriptor's ranges.
        var el = this._widgetEl(slot, module, cap);
        var min = null;
        var max = null;
        var step = null;
        if (el) {
          if (el.hasAttribute("data-min")) min = el.getAttribute("data-min");
          if (el.hasAttribute("data-max")) max = el.getAttribute("data-max");
          if (el.hasAttribute("data-step")) step = el.getAttribute("data-step");
        }
        if (min === null || max === null) {
          var descr = this._caps[key];
          if (descr && descr.ranges && descr.ranges.length) {
            if (min === null) min = descr.ranges[0].min;
            if (max === null) max = descr.ranges[descr.ranges.length - 1].max;
          }
          if (step === null && descr && descr.step != null) step = descr.step;
        }
        cache[key] = { min: min, max: max, step: step };
        return cache[key];
      },

      _widgetEl: function (slot, module, cap) {
        return document.querySelector(
          '[data-widget][data-slot="' +
            _cssEscape(slot) +
            '"][data-module="' +
            _cssEscape(module) +
            '"][data-cap="' +
            _cssEscape(cap) +
            '"]'
        );
      },

      _capType: function (slot, module, cap) {
        var descr = this._caps[L.widgetKey(slot, module, cap)];
        if (descr && descr.type) return descr.type;
        var el = this._widgetEl(slot, module, cap);
        return el ? el.getAttribute("data-type") : null;
      },

      // ---------------------------------------------------------------------
      // Commands
      // ---------------------------------------------------------------------
      setValue: function (slot, module, cap, rawValue) {
        var wkey = L.widgetKey(slot, module, cap);
        delete this.errors[wkey];
        var type = this._capType(slot, module, cap);
        var value;
        if (type === "bool") {
          value = !!rawValue;
        } else if (type === "float" || type === "int") {
          var n = L.parseNumber(rawValue);
          if (n === null) {
            this.errors[wkey] = L.errorMessage("bad_value");
            return;
          }
          value = type === "int" ? Math.round(n) : n;
        } else {
          // enum / string: send as-is (string).
          value = rawValue === undefined || rawValue === null ? "" : String(rawValue);
        }
        this._sendCommand(slot, module, cap, "set", value, wkey);
      },

      stepValue: function (slot, module, cap, dir) {
        var type = this._capType(slot, module, cap) || "int";
        var bounds = this._bounds(slot, module, cap);
        var current = this.values[L.widgetKey(slot, module, cap)];
        var next = L.computeStep(current, dir, bounds.step, bounds.min, bounds.max, type);
        if (next === null) return;
        this.setValue(slot, module, cap, String(next));
      },

      doAction: function (slot, module, cap) {
        this._sendCommand(slot, module, cap, "do", true, L.widgetKey(slot, module, cap));
      },

      _sendCommand: function (slot, module, cap, op, value, wkey) {
        if (!this._isOpen()) {
          this.errors[wkey] = L.errorMessage("timeout");
          return;
        }
        var rid = this._nextReqId();
        this._reqWidget[rid] = wkey;
        this.pending[wkey] = true;
        var ok = this._send({
          type: "command",
          request_id: rid,
          slot: L.slotAddr(slot),
          module: module,
          capability: cap,
          op: op,
          value: value,
        });
        if (!ok) {
          delete this.pending[wkey];
          delete this._reqWidget[rid];
          this.errors[wkey] = L.errorMessage("timeout");
        }
        if (cap === "tx_route") {
          var s = window.Alpine.store && window.Alpine.store("control");
          if (s) s.txRoute = value;
        }
      },

      _nextReqId: function () {
        this._reqSeq += 1;
        return "b" + this._reqSeq + "-" + Date.now();
      },

      // ---------------------------------------------------------------------
      // PTT
      // ---------------------------------------------------------------------
      pttState: function (slot, module) {
        return L.isKeyed(this.ptt[L.moduleKey(slot, module)]);
      },

      pttPhase: function (slot, module) {
        return this.ptt[L.moduleKey(slot, module)] || "armed";
      },

      pttDown: function (slot, module) {
        if (!this.canOperate(slot, module)) return;
        var mkey = L.moduleKey(slot, module);
        if (L.isKeyed(this.ptt[mkey])) return; // already keying/tx this module
        // One active PTT (MVP): unkey any other keyed module first.
        this._unkeyOthers(mkey);
        this.ptt[mkey] = L.nextPttPhase("armed", "down");
        this._sendPtt(slot, module, true);
        this._startKeepalive(slot, module, mkey);
        this._publishControlStore();
      },

      pttUp: function (slot, module) {
        var mkey = L.moduleKey(slot, module);
        if (!L.isKeyed(this.ptt[mkey])) return;
        this._unkeyModule(mkey, true);
      },

      // Unkey a single module: stop keepalive, send ptt=false (if requested and
      // socket open), phase -> armed.
      _unkeyModule: function (mkey, send) {
        this._stopKeepalive(mkey);
        this.ptt[mkey] = L.nextPttPhase(this.ptt[mkey] || "armed", "release");
        if (send && this._isOpen()) {
          var parts = mkey.split(" ");
          this._sendPtt(parts[0], parts.slice(1).join(" "), false);
        }
        this._publishControlStore();
      },

      _unkeyOthers: function (keepMkey) {
        for (var mkey in this.ptt) {
          if (!Object.prototype.hasOwnProperty.call(this.ptt, mkey)) continue;
          if (mkey === keepMkey) continue;
          if (L.isKeyed(this.ptt[mkey])) this._unkeyModule(mkey, true);
        }
      },

      _unkeyAll: function (send) {
        for (var mkey in this.ptt) {
          if (!Object.prototype.hasOwnProperty.call(this.ptt, mkey)) continue;
          if (L.isKeyed(this.ptt[mkey])) this._unkeyModule(mkey, send);
        }
      },

      _anyKeyed: function () {
        for (var mkey in this.ptt) {
          if (!Object.prototype.hasOwnProperty.call(this.ptt, mkey)) continue;
          if (L.isKeyed(this.ptt[mkey])) return true;
        }
        return false;
      },

      _sendPtt: function (slot, module, on) {
        // Route through _sendCommand so the request_id -> widget mapping is
        // recorded: result/error frames for the PTT command can then clear
        // pending state and surface errors, and isPending(...,ptt) is accurate.
        this._sendCommand(slot, module, PTT_CAP, "do", !!on, L.widgetKey(slot, module, PTT_CAP));
      },

      _startKeepalive: function (slot, module, mkey) {
        this._stopKeepalive(mkey);
        var self = this;
        this._keepaliveMap = this._keepaliveMap || {};
        this._keepaliveMap[mkey] = setInterval(function () {
          if (!self._isOpen() || !L.isKeyed(self.ptt[mkey])) {
            self._stopKeepalive(mkey);
            return;
          }
          self._send({ type: "ptt_keepalive", slot: L.slotAddr(slot), module: module });
        }, KEEPALIVE_MS);
      },

      _stopKeepalive: function (mkey) {
        if (this._keepaliveMap && this._keepaliveMap[mkey]) {
          clearInterval(this._keepaliveMap[mkey]);
          delete this._keepaliveMap[mkey];
        }
      },

      _firstPttModule: function () {
        for (var mkey in this._capNames) {
          if (!Object.prototype.hasOwnProperty.call(this._capNames, mkey)) continue;
          var caps = this._caps;
          var parts = mkey.split(" ");
          var slot = parts[0];
          var module = parts.slice(1).join(" ");
          if (caps[L.widgetKey(slot, module, PTT_CAP)]) {
            return { slot: slot, module: module };
          }
        }
        return null;
      },

      // ---------------------------------------------------------------------
      // Lock methods (templates + banner buttons)
      // ---------------------------------------------------------------------
      acquire: function () {
        this._send({ type: "lock_acquire" });
      },
      release: function () {
        this._send({ type: "lock_release" });
      },
      request: function () {
        this._send({ type: "lock_request" });
      },
      preempt: function () {
        this._send({ type: "lock_preempt" });
      },
      grant: function (userId) {
        if (userId === undefined || userId === null) return;
        this._send({ type: "lock_transfer", to_user_id: userId });
        this.pendingRequest = null;
      },
      dismissRequest: function () {
        this.pendingRequest = null;
      },

      // ---------------------------------------------------------------------
      // Global listeners (keyboard PTT + fail-safe blur/visibility)
      // ---------------------------------------------------------------------
      _installListeners: function () {
        var self = this;
        var onKeyDown = function (e) {
          if (L.shouldIgnoreKey(e, document.activeElement, self.pttKey)) return;
          if (!self.canControl) return;
          e.preventDefault(); // stop page scroll on Space
          var t = self._firstPttModule();
          if (t) self.pttDown(t.slot, t.module);
        };
        var onKeyUp = function (e) {
          if (!L.isPttKey(e, self.pttKey)) return;
          var t = self._firstPttModule();
          if (t) self.pttUp(t.slot, t.module);
        };
        var onBlur = function () {
          self._unkeyAll(true);
        };
        var onVisibility = function () {
          if (document.visibilityState === "hidden") self._unkeyAll(true);
        };
        var onBeforeUnload = function () {
          self._teardown();
        };
        this._addListener(document, "keydown", onKeyDown);
        this._addListener(document, "keyup", onKeyUp);
        this._addListener(window, "blur", onBlur);
        this._addListener(document, "visibilitychange", onVisibility);
        this._addListener(window, "beforeunload", onBeforeUnload);
      },

      _addListener: function (target, type, handler) {
        target.addEventListener(type, handler);
        this._listeners.push({ target: target, type: type, handler: handler });
      },

      _teardown: function () {
        if (this._closed) return;
        this._closed = true;
        this._unsubscribeAll();
        this._unkeyAll(true);
        for (var mkey in this._keepaliveMap || {}) {
          this._stopKeepalive(mkey);
        }
        if (this._lossTimer) clearTimeout(this._lossTimer);
        for (var i = 0; i < this._listeners.length; i++) {
          var l = this._listeners[i];
          try {
            l.target.removeEventListener(l.type, l.handler);
          } catch (_) {}
        }
        this._listeners = [];
        if (this._ws) {
          try {
            this._ws.close();
          } catch (_) {}
          this._ws = null;
        }
      },
    };
  }

  // CSS.escape polyfill-ish guard for attribute-selector safety.
  function _cssEscape(s) {
    s = String(s);
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(s);
    }
    return s.replace(/["\\\]]/g, "\\$&");
  }

  document.addEventListener("alpine:init", function () {
    window.Alpine.store("control", {
      stationId: null,
      youHold: false,
      keyed: false,
      canControl: false,
      connected: false,
      txRoute: null,
      _send: null, // set by the controlPanel init
      sendCommand: function (obj) {
        return typeof this._send === "function" ? this._send(obj) : false;
      },
    });
    window.Alpine.data("controlPanel", controlPanel);
  });
})();
