/* OE5XRX Control Panel — pure logic
   No DOM, no Alpine, no WebSocket. Every function here is deterministic and
   unit-tested from Node (tests/js/control-logic.test.mjs). control-panel.js
   consumes this via window.OE5XRXControlLogic; Node consumes it via require().

   UMD: attaches to module.exports when present, else window.OE5XRXControlLogic.
   Style mirrors static/js/app.js — ES5-ish, "use strict", var/function. */

(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.OE5XRXControlLogic = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // §7 envelope
  // ---------------------------------------------------------------------------

  // Protocol version carried on EVERY browser->server frame (design spec §7).
  // The agent's parse_message drops any frame whose "v" != PROTOCOL_VERSION,
  // so an unstamped command/subscribe/ptt_keepalive is silently rejected and
  // the browser only ever sees a command timeout ("No response").
  var PROTOCOL_VERSION = 1;

  /* Return a shallow copy of `obj` with the §7 envelope version FORCED to
     PROTOCOL_VERSION. A caller-supplied `v` is always overridden — there is
     exactly one valid version, and a wrong `v` would be silently dropped by
     the agent, so we never trust the caller here. */
  function envelope(obj) {
    var out = {};
    for (var k in obj) {
      if (Object.prototype.hasOwnProperty.call(obj, k)) out[k] = obj[k];
    }
    out.v = PROTOCOL_VERSION;
    return out;
  }

  /* Coerce a slot address to the wire type the agent expects. The slot contract
     (/dev/oe5xrx/slotN/control) makes slots integers, and the agent's broker
     rejects a non-int slot ("malformed addr"). But the slot round-trips through
     StationModule.slot (a Django CharField) and the widget's data-slot HTML
     attribute, both of which stringify it — so D5 would otherwise send "1" and
     be rejected. Return a Number for an all-digits slot; leave anything else
     untouched (the agent would reject a genuinely non-numeric slot anyway). */
  function slotAddr(slot) {
    var s = String(slot);
    return /^\d+$/.test(s) ? Number(s) : slot;
  }

  // ---------------------------------------------------------------------------
  // DE-locale number parsing / serialization
  // ---------------------------------------------------------------------------

  /* Parse a user- or server-supplied numeric value defensively. Accepts a
     comma OR dot decimal separator (de_AT keyboards emit comma) and returns a
     JS number, or null when the value is not a finite number. Callers MUST
     reject null and never send it to the server. */
  function parseNumber(raw) {
    if (raw === null || raw === undefined) return null;
    if (typeof raw === "number") return isFinite(raw) ? raw : null;
    var s = String(raw).trim();
    if (s === "") return null;
    // Only the decimal separator may be a comma; a value like "1.234,5"
    // (thousands) is ambiguous and rejected — inputs are dot-decimal (lang=en).
    s = s.replace(",", ".");
    var n = parseFloat(s);
    if (!isFinite(n)) return null;
    // parseFloat("1.2x") === 1.2 — guard against trailing garbage.
    if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(s)) return null;
    return n;
  }

  /* Serialize a number for the wire / display as a dot-decimal string with no
     exponent and no comma. Integers render without a fractional part. */
  function formatNumber(n) {
    if (n === null || n === undefined || !isFinite(n)) return "";
    if (Number.isInteger(n)) return String(n);
    // Avoid exponent notation for the magnitudes we handle (frequencies etc.).
    var s = String(n);
    if (s.indexOf("e") !== -1 || s.indexOf("E") !== -1) {
      // Fall back to a fixed representation, trimming trailing zeros.
      s = n.toFixed(6).replace(/\.?0+$/, "");
    }
    return s.replace(",", ".");
  }

  // ---------------------------------------------------------------------------
  // PTT keyboard guards
  // ---------------------------------------------------------------------------

  /* True when a keydown/keyup should NOT trigger PTT:
       - auto-repeat (event.repeat) — hold must not spam key/unkey,
       - the user is typing in a form field / contenteditable,
       - the key is not the configured PTT key.
     activeEl is document.activeElement; pttKey is the configured key (Space=' '). */
  function shouldIgnoreKey(event, activeEl, pttKey) {
    if (!event) return true;
    if (event.repeat) return true;
    if (isTypingTarget(activeEl)) return true;
    if (!isPttKey(event, pttKey)) return true;
    return false;
  }

  function isTypingTarget(el) {
    if (!el) return false;
    if (el.isContentEditable) return true;
    var tag = el.tagName ? String(el.tagName).toUpperCase() : "";
    return tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
  }

  function isPttKey(event, pttKey) {
    if (!event) return false;
    var key = event.key;
    if (key === undefined || key === null) return false;
    // Space is represented as ' ' in KeyboardEvent.key; some engines report
    // 'Spacebar'. Accept both when the configured key is a space.
    if (pttKey === " ") return key === " " || key === "Spacebar";
    return key === pttKey;
  }

  // ---------------------------------------------------------------------------
  // PTT state machine — armed -> keying -> tx, release -> armed
  // ---------------------------------------------------------------------------

  /* Pure transition. Actions: 'down' (key pressed), 'confirm' (agent state
     ptt===true), 'release' (any unkey path). Unknown actions are no-ops. */
  function nextPttPhase(phase, action) {
    switch (action) {
      case "down":
        return phase === "armed" ? "keying" : phase;
      case "confirm":
        return phase === "keying" ? "tx" : phase;
      case "release":
        return "armed";
      default:
        return phase;
    }
  }

  function isKeyed(phase) {
    return phase === "keying" || phase === "tx";
  }

  // ---------------------------------------------------------------------------
  // Telemetry percentage
  // ---------------------------------------------------------------------------

  /* Map value into 0..100 across [min,max]. Missing/degenerate range -> 0. */
  function telemetryPercent(value, min, max) {
    var v = parseNumber(value);
    var lo = parseNumber(min);
    var hi = parseNumber(max);
    if (v === null || lo === null || hi === null || hi <= lo) return 0;
    var pct = ((v - lo) / (hi - lo)) * 100;
    if (pct < 0) return 0;
    if (pct > 100) return 100;
    return pct;
  }

  // ---------------------------------------------------------------------------
  // Numeric step + clamp
  // ---------------------------------------------------------------------------

  /* Compute the next value when stepping a numeric widget. current may be a
     string (from the input) or number; dir is +1/-1; step defaults per type.
     Clamps to [min,max] when finite. Returns a number, or null if current is
     unparseable and there is no min to anchor from. */
  function computeStep(current, dir, step, min, max, type) {
    var base = parseNumber(current);
    var s = parseNumber(step);
    if (s === null || s <= 0) s = type === "int" ? 1 : 0.1;
    if (base === null) {
      var lo = parseNumber(min);
      if (lo === null) return null;
      base = lo;
    } else {
      base = base + dir * s;
    }
    base = clamp(base, min, max);
    if (type === "int") base = Math.round(base);
    // Kill float dust from repeated 0.1 additions (0.30000000000000004).
    base = Math.round(base * 1e6) / 1e6;
    return base;
  }

  function clamp(v, min, max) {
    var lo = parseNumber(min);
    var hi = parseNumber(max);
    if (lo !== null && v < lo) v = lo;
    if (hi !== null && v > hi) v = hi;
    return v;
  }

  // ---------------------------------------------------------------------------
  // Lock-state derivation
  // ---------------------------------------------------------------------------

  /* Map the server lock frame's {state, you_hold} to the UI's tri-state used
     by the banner CSS (is-you / is-other / is-free). */
  function deriveLockState(state, youHold) {
    if (state !== "held") return "free";
    return youHold ? "held" : "other";
  }

  // ---------------------------------------------------------------------------
  // Structured command-error code -> human string
  // ---------------------------------------------------------------------------

  var ERROR_MESSAGES = {
    out_of_range: "Out of range",
    bad_value: "Invalid value",
    read_only: "Read-only",
    wrong_op: "Not allowed",
    not_locked: "You don’t have control",
    timeout: "No response",
    forbidden: "Not permitted",
  };

  function errorMessage(code) {
    if (!code) return "";
    return ERROR_MESSAGES[code] || String(code);
  }

  // ---------------------------------------------------------------------------
  // Subscription interval — respect descriptor floor, clamp to a sane minimum
  // ---------------------------------------------------------------------------

  /* From an array of telemetry cap descriptors, derive the subscribe interval:
     honor the largest min_interval_ms floor present (slowest wins so we never
     ask faster than any cap allows), default 500ms, never below 250ms. */
  function subscribeInterval(caps) {
    var floor = 0;
    if (caps && caps.length) {
      for (var i = 0; i < caps.length; i++) {
        var mi = caps[i] && caps[i].min_interval_ms;
        if (typeof mi === "number" && isFinite(mi) && mi > floor) floor = mi;
      }
    }
    var interval = floor > 0 ? floor : 500;
    return interval < 250 ? 250 : interval;
  }

  // ---------------------------------------------------------------------------
  // Composite key for the value/telemetry/pending/error maps
  // ---------------------------------------------------------------------------

  function widgetKey(slot, module, cap) {
    return String(slot) + " " + String(module) + " " + String(cap);
  }

  function moduleKey(slot, module) {
    return String(slot) + " " + String(module);
  }

  return {
    PROTOCOL_VERSION: PROTOCOL_VERSION,
    envelope: envelope,
    slotAddr: slotAddr,
    parseNumber: parseNumber,
    formatNumber: formatNumber,
    shouldIgnoreKey: shouldIgnoreKey,
    isTypingTarget: isTypingTarget,
    isPttKey: isPttKey,
    nextPttPhase: nextPttPhase,
    isKeyed: isKeyed,
    telemetryPercent: telemetryPercent,
    computeStep: computeStep,
    clamp: clamp,
    deriveLockState: deriveLockState,
    errorMessage: errorMessage,
    subscribeInterval: subscribeInterval,
    widgetKey: widgetKey,
    moduleKey: moduleKey,
  };
});
