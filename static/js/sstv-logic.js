/* OE5XRX SSTV — pure helpers of the control-page SSTV section (SSTV spec §5.3/§5.4).
   No DOM, no Alpine: sstv-panel.js is the stateful shell. Exported as
   window.OE5XRXSstvLogic and via module.exports for the Node test suite.
   Style follows scanner-panel.js (ES5-ish IIFE). */

(function () {
  "use strict";

  // i18n: globals from Django's JavaScriptCatalog; identity fallback under Node.
  // Function-scoped so classic <script> files never collide.
  var gettext = globalThis.gettext || function (s) { return s; };
  var interpolate = globalThis.interpolate || function (fmt, obj, named) {
    if (named) return fmt.replace(/%\(\s*([^)]+)\s*\)s/g, function (m, k) { return String(obj[k]); });
    return fmt.replace(/%s/g, function () { return String(obj.shift()); });
  };

  // Same constants as station_agent/sstv/modes.py (ISS_FREQUENCY_HZ, ISS_GUARD_HZ).
  var ISS_HZ = 145800000;
  var ISS_GUARD_HZ = 10000;
  var RX_CAP = "rx_frequency";
  var TX_CAP = "tx_frequency";

  function has(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  // -------------------------------------------------------------------------
  // Live lines
  // -------------------------------------------------------------------------
  function base64Bytes(b64) {
    if (typeof b64 !== "string" || !b64) return new Uint8Array(0);
    try {
      if (typeof globalThis.atob === "function") {
        var bin = globalThis.atob(b64);
        var out = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
        return out;
      }
    } catch (_) {
      return new Uint8Array(0);
    }
    return new Uint8Array(0);
  }

  // RGB row (base64, 3 bytes per pixel) -> RGBA pixels for ImageData. A trailing
  // partial pixel is dropped; garbage decodes to an empty row.
  function decodeRow(b64) {
    var rgb = base64Bytes(b64);
    var px = Math.floor(rgb.length / 3);
    var out = new Uint8ClampedArray(px * 4);
    for (var i = 0, j = 0; i < px; i++, j += 3) {
      out[i * 4] = rgb[j];
      out[i * 4 + 1] = rgb[j + 1];
      out[i * 4 + 2] = rgb[j + 2];
      out[i * 4 + 3] = 255;
    }
    return out;
  }

  // -------------------------------------------------------------------------
  // Time / frequency
  // -------------------------------------------------------------------------
  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  // Seconds as "mm:ss" (minutes not wrapped: 30 min = "30:00"); never negative.
  function formatSeconds(seconds) {
    var s = Math.max(0, Math.ceil(Number(seconds) || 0));
    return pad2(Math.floor(s / 60)) + ":" + pad2(s % 60);
  }

  // Time left until ``endsAtIso`` at ``nowMs`` as "mm:ss"; "" without a valid end.
  function formatRemaining(endsAtIso, nowMs) {
    if (!endsAtIso) return "";
    var end = Date.parse(endsAtIso);
    if (!isFinite(end)) return "";
    return formatSeconds((end - Number(nowMs)) / 1000);
  }

  function isIssFrequency(hz) {
    var n = Number(hz);
    if (hz === null || hz === undefined || hz === "" || !isFinite(n)) return false;
    return Math.abs(n - ISS_HZ) <= ISS_GUARD_HZ;
  }

  // Module setting in MHz (agent reports MHz) -> Hz, or null.
  function mhzToHz(value) {
    if (typeof value !== "number" || !isFinite(value) || value <= 0) return null;
    return Math.round(value * 1000000);
  }

  function formatMHz(hz) {
    if (hz === null || hz === undefined || !isFinite(Number(hz))) return "";
    return (Number(hz) / 1000000).toFixed(4);
  }

  // -------------------------------------------------------------------------
  // Inventory
  // -------------------------------------------------------------------------
  // Online modules of numeric slots that can tune RX and/or TX:
  // [{slot, module, rx: bool, tx: bool}] in inventory order.
  function radioModules(slots) {
    var out = [];
    if (!Array.isArray(slots)) return out;
    for (var i = 0; i < slots.length; i++) {
      var slot = slots[i] && slots[i].slot;
      if (slot === undefined || slot === null || !/^\d+$/.test(String(slot))) continue;
      var mods = (slots[i] && slots[i].modules) || [];
      for (var j = 0; j < mods.length; j++) {
        var m = mods[j];
        if (!m || m.online === false || typeof m.module !== "string") continue;
        var caps = Array.isArray(m.capabilities) ? m.capabilities : [];
        var rx = false;
        var tx = false;
        for (var c = 0; c < caps.length; c++) {
          if (!caps[c]) continue;
          if (caps[c].name === RX_CAP) rx = true;
          if (caps[c].name === TX_CAP) tx = true;
        }
        if (rx || tx) out.push({ slot: String(slot), module: m.module, rx: rx, tx: tx });
      }
    }
    return out;
  }

  // -------------------------------------------------------------------------
  // Texts
  // -------------------------------------------------------------------------
  // Refusals of preview, RX start/stop and send (server codes; texts are ours).
  var GUARD = {
    not_locked: gettext("Take control of the station first."),
    forbidden: gettext("Your membership level may not use SSTV."),
    unsupported: gettext("The station software is too old for SSTV."),
    invalid_slot: gettext("This module cannot receive."),
    sstv_busy: gettext("A picture is being sent. Wait until it has finished."),
    not_found: gettext("The preview is gone. Create a new preview."),
    expired: gettext("The preview is older than one hour. Create a new preview."),
    no_callsign: gettext("The picture carries no callsign. Confirm to send it anyway."),
    module_offline: gettext("The module is offline."),
    no_frequency: gettext("The module has not reported its TX frequency yet."),
    iss_downlink: gettext("Sending on the ISS downlink (145.800 MHz) is not allowed. Change the TX frequency."),
    out_of_range: gettext("The TX frequency is outside the module's TX range."),
    aborted: gettext("The transmission was aborted."),
    scan_active: gettext("Stop the scanner before sending a picture."),
    ptt_active: gettext("Release PTT before sending a picture."),
    no_active_sstv: gettext("Nothing is running."),
    not_connected: gettext("The station connection is down. Wait for it to reconnect."),
    timeout: gettext("The station did not answer in time."),
    bad_mode: gettext("Choose an SSTV mode."),
    bad_source: gettext("Choose a photo or the test picture."),
    no_file: gettext("Choose a photo to send."),
    no_module: gettext("No module of this station can transmit."),
    internal_error: gettext("SSTV failed on the server. Try again."),
  };

  function sendGuardMessage(code) {
    return has(GUARD, code) ? GUARD[code] : gettext("The request was refused.");
  }

  // Why receive ended (sstv_state idle/error ``detail``); "" = nothing to explain.
  function rxEndText(state, detail) {
    var d = typeof detail === "string" ? detail : "";
    if (d === "operator" || d === "stopped" || d === "") {
      return state === "error" ? gettext("Receiving stopped because of an error.") : "";
    }
    if (d === "max_runtime") return gettext("Receiving ended after 30 minutes. Start again to continue.");
    if (d === "scan started") return gettext("Receiving stopped because a scan was started.");
    if (d.indexOf("lock ") === 0) return gettext("Receiving stopped because the lock changed hands.");
    if (d === "agent_restarted" || d === "agent_disconnect" || d === "ws_disconnect") {
      return gettext("Receiving stopped because the station connection was lost.");
    }
    if (has(GUARD, d)) return GUARD[d];
    return gettext("Receiving stopped because of an error.");
  }

  // End of a transmission (sstv_tx_state aborted/error ``detail``).
  // ``mine``: the reader's own transmission (only then "you" wording).
  function txEndText(state, detail, mine) {
    var d = typeof detail === "string" ? detail : "";
    if (state === "done") return gettext("Picture sent. It is in the gallery.");
    if (d === "operator") return mine ? gettext("You aborted the transmission.") : GUARD.aborted;
    if (d.indexOf("lock ") === 0) return gettext("The transmission was aborted because the lock changed hands.");
    if (d === "ws_disconnect" || d === "agent_disconnect") {
      return gettext("The transmission was aborted because the station connection was lost.");
    }
    if (d === "iss_downlink" || d === "out_of_range" || d === "ptt_active") return GUARD[d];
    if (state === "aborted") return gettext("The transmission was aborted.");
    return gettext("The transmission failed. The station unkeyed.");
  }

  var UPLOAD_TYPES = { "image/jpeg": true, "image/png": true, "image/webp": true, "image/gif": true };

  // Client-side copy of the server's upload checks (render.load_upload): the type the
  // browser reports and the size. "" = fine; the server checks again (and the content).
  function uploadRefusal(file, maxBytes) {
    if (!file) return GUARD.no_file;
    if (file.type && !UPLOAD_TYPES[file.type]) {
      return gettext("The file is not a JPEG, PNG, WebP or GIF picture.");
    }
    if (maxBytes > 0 && file.size > maxBytes) {
      return interpolate(gettext("The file is larger than %(mb)s MB."), { mb: Math.floor(maxBytes / 1048576) }, true);
    }
    return "";
  }

  var TX_PHASES = {
    sending: gettext("Preparing"),
    downloading: gettext("Loading the picture onto the station"),
    keyed: gettext("On the air"),
  };

  function txPhaseText(state) {
    return has(TX_PHASES, state) ? TX_PHASES[state] : "";
  }

  var api = {
    ISS_HZ: ISS_HZ,
    decodeRow: decodeRow,
    formatSeconds: formatSeconds,
    formatRemaining: formatRemaining,
    isIssFrequency: isIssFrequency,
    mhzToHz: mhzToHz,
    formatMHz: formatMHz,
    radioModules: radioModules,
    sendGuardMessage: sendGuardMessage,
    rxEndText: rxEndText,
    txEndText: txEndText,
    txPhaseText: txPhaseText,
    uploadRefusal: uploadRefusal,
  };
  if (typeof window !== "undefined") window.OE5XRXSstvLogic = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
