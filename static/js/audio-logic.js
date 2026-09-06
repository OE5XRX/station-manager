/* OE5XRX Audio Client — pure logic
   No DOM, no Alpine, no WebSocket. Every function here is deterministic and
   unit-tested from Node (tests/js/audio-logic.test.mjs). The Alpine audio
   component consumes this via window.OE5XRXAudioLogic; Node consumes it via
   require().

   UMD: attaches to module.exports when present, else window.OE5XRXAudioLogic.
   Style mirrors static/js/control-logic.js — ES5-ish, "use strict", var/function. */

(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.OE5XRXAudioLogic = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // §5.3 frame codec constants
  // ---------------------------------------------------------------------------

  var MAGIC = 0xA5;
  var VERSION = 1;
  var HEADER_LEN = 12;
  var FLAG_FEC = 0x01;
  var FLAG_DTX = 0x02;
  var FLAG_MARKER = 0x04;
  var AUDIO_PROTOCOL_VERSION = 1;
  // Fixed scale from mic time-domain RMS to a 0..1 meter level (see
  // micLevelFromRms). Kept here so the JS component and its Node test agree.
  var MIC_LEVEL_SCALE = 4;

  // ---------------------------------------------------------------------------
  // FrameError
  // ---------------------------------------------------------------------------

  function FrameError(message) {
    this.message = message || "FrameError";
    this.name = "FrameError";
    if (Error.captureStackTrace) {
      Error.captureStackTrace(this, FrameError);
    } else {
      this.stack = (new Error(message)).stack;
    }
  }
  FrameError.prototype = Object.create(Error.prototype);
  FrameError.prototype.constructor = FrameError;

  // ---------------------------------------------------------------------------
  // §5.3 packFrame / parseFrame — DataView-based, browser + Node compatible
  // ---------------------------------------------------------------------------

  /* Serialize one §5.3 media frame.
     stream_ref/seq masked to u16, ts masked to u32.
     Returns a Uint8Array of 12 + payload.length bytes.
     Uses DataView (LE) — no Node Buffer dependency. */
  function packFrame(opts) {
    var stream_ref = (opts.stream_ref & 0xFFFF);
    var seq = (opts.seq & 0xFFFF);
    var ts = (opts.ts >>> 0);
    var flags = (opts.flags || 0) & 0xFF;
    var payload = opts.payload || new Uint8Array(0);

    var buf = new ArrayBuffer(HEADER_LEN + payload.length);
    var view = new DataView(buf);
    var out = new Uint8Array(buf);

    view.setUint8(0, MAGIC);
    view.setUint8(1, VERSION);
    view.setUint16(2, stream_ref, true);  // LE
    view.setUint16(4, seq, true);          // LE
    view.setUint32(6, ts, true);           // LE
    view.setUint8(10, flags);
    view.setUint8(11, 0);                  // reserved

    // Copy payload bytes at offset 12
    for (var i = 0; i < payload.length; i++) {
      out[HEADER_LEN + i] = payload[i];
    }

    return out;
  }

  /* Parse one §5.3 media frame. bytes may be Uint8Array or ArrayBuffer.
     Returns {stream_ref, seq, ts, flags, reserved, payload, fec, dtx, marker}
     or throws FrameError on bad magic / bad version / too short. */
  function parseFrame(bytes) {
    var buf;
    if (bytes instanceof ArrayBuffer) {
      buf = bytes;
    } else {
      // Uint8Array or similar — get the underlying buffer slice
      buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    }

    if (buf.byteLength < HEADER_LEN) {
      throw new FrameError("frame too short: " + buf.byteLength + " < " + HEADER_LEN);
    }

    var view = new DataView(buf);
    var magic = view.getUint8(0);
    var ver = view.getUint8(1);

    if (magic !== MAGIC) {
      throw new FrameError("bad magic 0x" + magic.toString(16));
    }
    if (ver !== VERSION) {
      throw new FrameError("unsupported frame version " + ver);
    }

    var stream_ref = view.getUint16(2, true);  // LE
    var seq = view.getUint16(4, true);          // LE
    var ts = view.getUint32(6, true);           // LE
    var flags = view.getUint8(10);
    var reserved = view.getUint8(11);

    // Return payload as a copy (Uint8Array), not a view into the buffer
    var payloadLen = buf.byteLength - HEADER_LEN;
    var payload = new Uint8Array(payloadLen);
    var src = new Uint8Array(buf, HEADER_LEN, payloadLen);
    payload.set(src);

    return {
      stream_ref: stream_ref,
      seq: seq,
      ts: ts,
      flags: flags,
      reserved: reserved,
      payload: payload,
      fec: !!(flags & FLAG_FEC),
      dtx: !!(flags & FLAG_DTX),
      marker: !!(flags & FLAG_MARKER),
    };
  }

  // ---------------------------------------------------------------------------
  // DE-locale number parsing (inline fallback — no control-logic dependency)
  // ---------------------------------------------------------------------------

  /* Parse a user- or server-supplied numeric value defensively.
     Accepts comma OR dot decimal separator (de_AT keyboards emit comma).
     Returns a JS number, or null when the value is not a finite number.
     Inlined (identical to OE5XRXControlLogic.parseNumber) so this module has
     exactly one code path and no dependency on control-logic.js load order —
     it must run standalone in Node where that global never exists. */
  function _parseNumber(raw) {
    if (raw === null || raw === undefined) return null;
    if (typeof raw === "number") return isFinite(raw) ? raw : null;
    var s = String(raw).trim();
    if (s === "") return null;
    s = s.replace(",", ".");
    var n = parseFloat(s);
    if (!isFinite(n)) return null;
    if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(s)) return null;
    return n;
  }

  // ---------------------------------------------------------------------------
  // Stream index
  // ---------------------------------------------------------------------------

  /* Build bi-directional index from a streams message (type:"streams").
     Ignores entries missing stream_id or stream_ref.
     Returns {byId:{[stream_id]:entry}, byRef:{[stream_ref]:stream_id}, list:[entry]}. */
  function buildStreamIndex(streamsMsg) {
    var streams = (streamsMsg && streamsMsg.streams) || [];
    var byId = {};
    var byRef = {};
    var list = [];

    for (var i = 0; i < streams.length; i++) {
      var entry = streams[i];
      if (entry.stream_id === undefined || entry.stream_id === null ||
          entry.stream_ref === undefined || entry.stream_ref === null) {
        continue;
      }
      byId[entry.stream_id] = entry;
      byRef[entry.stream_ref] = entry.stream_id;
      list.push(entry);
    }

    return { byId: byId, byRef: byRef, list: list };
  }

  // ---------------------------------------------------------------------------
  // Stream predicates
  // ---------------------------------------------------------------------------

  /* True when entry is the operator mic input. */
  function isOpMic(entry) {
    return entry.module === "operator" || entry.stream_id === "op.mic";
  }

  /* True when entry is a receive-direction source that is not the op mic. */
  function isRxSource(entry) {
    return entry.direction === "rx" && !isOpMic(entry);
  }

  // ---------------------------------------------------------------------------
  // Presets
  // ---------------------------------------------------------------------------

  var PRESETS = ["fm", "satellite", "custom"];

  /* Return sorted stream_id[] for the given preset.
     fm = all RX sources + op.mic
     satellite = all RX sources, no op.mic
     custom = [] (caller supplies explicit set) */
  function presetSubscriptions(preset, list) {
    var ids = [];
    if (preset === "custom") {
      return ids;
    }
    for (var i = 0; i < list.length; i++) {
      var entry = list[i];
      if (preset === "fm") {
        if (isRxSource(entry) || isOpMic(entry)) {
          ids.push(entry.stream_id);
        }
      } else if (preset === "satellite") {
        if (isRxSource(entry)) {
          ids.push(entry.stream_id);
        }
      }
    }
    return ids.sort();
  }

  // ---------------------------------------------------------------------------
  // Mixer
  // ---------------------------------------------------------------------------

  /* Default mixer entry: unity gain, not muted. */
  function defaultMixerEntry() {
    return { gainDb: 0, muted: false };
  }

  /* Convert dB to linear gain. Floors to 0 at or below -60 dB. */
  function dbToLinear(db) {
    if (db <= -60) return 0;
    return Math.pow(10, db / 20);
  }

  /* Clamp a gain value (string or number, comma/dot decimal) to [-60, 12] dB.
     Parses comma decimal (DE locale). null/NaN/"abc" → 0. */
  function clampGainDb(raw) {
    var n = _parseNumber(raw);
    if (n === null) return 0;
    if (n > 12) return 12;
    if (n < -60) return -60;
    return n;
  }

  /* Return linear gain: 0 when muted, dbToLinear(gainDb) otherwise. */
  function effectiveGain(entry) {
    if (entry.muted) return 0;
    return dbToLinear(entry.gainDb);
  }

  // ---------------------------------------------------------------------------
  // Uplink coupling
  // ---------------------------------------------------------------------------

  /* True when the mic should open the uplink:
     all three conditions must hold simultaneously. */
  function micWantsUplink(opts) {
    return !!(opts.micEnabled && opts.keyed && opts.youHold);
  }

  /* Map a raw time-domain RMS (0..~1) to a 0..1 meter level for the local mic
     input indicator. Speech RMS sits well below 1.0, so a fixed scale (×4)
     makes normal talking fill a useful fraction of the bar without clipping
     instantly. Clamped to [0, 1]; non-finite / negative input → 0. */
  function micLevelFromRms(rms) {
    if (typeof rms !== "number" || !isFinite(rms) || rms <= 0) return 0;
    var v = rms * MIC_LEVEL_SCALE;
    return v > 1 ? 1 : v;
  }

  // ---------------------------------------------------------------------------
  // Seq math — u16 wrap-aware
  // ---------------------------------------------------------------------------

  /* Shortest signed distance from prev to next in u16 space.
     +1 in-order, 0 duplicate, -1 late/reorder.
     Wrap: seqDelta(65535, 0) === 1, seqDelta(0, 65535) === -1. */
  function seqDelta(prev, next) {
    var d = ((next - prev) & 0xFFFF);
    return d > 0x8000 ? d - 0x10000 : d;
  }

  // ---------------------------------------------------------------------------
  // Jitter buffer
  // ---------------------------------------------------------------------------

  /* Create jitter buffer state.
     depth = max reorder window in frames (default 3). */
  function createJitter(opts) {
    var depth = (opts && opts.depth !== undefined) ? opts.depth : 3;
    return {
      depth: depth,
      frames: {},       // seq -> frame
      next: null,       // next expected seq to emit (initialized on first drain)
      started: false,
    };
  }

  /* Push a {seq, frame} into the jitter buffer.
     Drops duplicates and too-late frames (seq < state.next, wrap-aware).
     Returns {accepted: bool}. */
  function jitterPush(state, item) {
    var seq = item.seq;

    // If we have started (next initialized), drop frames behind the window.
    // seqDelta(seq, state.next) > 0 means state.next is ahead of seq (seq is in the past).
    if (state.next !== null) {
      if (seqDelta(seq, state.next) > 0) {
        return { accepted: false };
      }
    }

    // Drop duplicates already in the buffer
    if (Object.prototype.hasOwnProperty.call(state.frames, seq)) {
      return { accepted: false };
    }

    state.frames[seq] = item.frame;
    return { accepted: true };
  }

  /* Drain ready frames from the jitter buffer.
     Returns {out:[{seq, frame, plc}...], gaps:[{seq, count}]}.

     Logic:
       - On first call, initialize state.next to the lowest buffered seq.
       - keep = max(1, depth - 1): the number of frames held back as the reorder window.
       - While actual frame count in buffer > keep:
           * If frames[next] exists: emit it, remove from buffer, advance next.
           * If frames[next] missing AND highest buffered seq is more than depth
             ahead of next (gap confirmed lost): emit PLC placeholder, record gap,
             advance next (PLC does NOT remove a frame from the count).
           * Otherwise: break (gap still within reorder window, wait for more).
       - Consecutive PLC seqs are coalesced into a single gap entry with count > 1.
  */
  function jitterDrain(state) {
    var out = [];
    var gaps = [];

    // Initialize next to the lowest buffered seq on first drain
    if (state.next === null) {
      var initSeqs = _bufferedSeqs(state);
      if (initSeqs.length === 0) {
        return { out: out, gaps: gaps };
      }
      state.next = initSeqs[0];
      state.started = true;
    }

    // How many frames to keep buffered (reorder window)
    var keep = Math.max(1, state.depth - 1);

    while (true) {
      // Count actual frames in buffer
      var count = Object.keys(state.frames).length;

      if (count <= keep) break;

      if (Object.prototype.hasOwnProperty.call(state.frames, state.next)) {
        // Frame available: emit and remove
        var frame = state.frames[state.next];
        delete state.frames[state.next];
        out.push({ seq: state.next, frame: frame, plc: false });
      } else {
        // Frame missing — check if the gap is confirmed lost (beyond depth)
        var buffered = _bufferedSeqs(state);
        if (buffered.length === 0) break;
        var highest = buffered[buffered.length - 1];
        if (seqDelta(state.next, highest) <= state.depth) {
          // Gap is within the reorder window — wait for potential late arrival
          break;
        }
        // Gap confirmed lost: emit PLC placeholder (does not consume a buffer slot)
        out.push({ seq: state.next, frame: null, plc: true });
        // Coalesce consecutive gap seqs
        if (gaps.length > 0) {
          var lastGap = gaps[gaps.length - 1];
          // Check if this PLC seq directly follows the previous gap
          if (((lastGap.seq + lastGap.count) & 0xFFFF) === state.next) {
            lastGap.count += 1;
          } else {
            gaps.push({ seq: state.next, count: 1 });
          }
        } else {
          gaps.push({ seq: state.next, count: 1 });
        }
        // NOTE: count does NOT decrease (no frame removed), loop re-checks count > keep
        // This is fine: the PLC advances next, and on the next iteration we'll emit
        // the next real frame or another PLC, until a real frame brings count down.
      }

      state.next = (state.next + 1) & 0xFFFF;
    }

    return { out: out, gaps: gaps };
  }

  /* Return sorted array of currently buffered seq numbers, sorted by
     wrap-aware distance from state.next (ascending, i.e. oldest first). */
  function _bufferedSeqs(state) {
    var keys = Object.keys(state.frames);
    var nums = [];
    for (var i = 0; i < keys.length; i++) {
      nums.push(Number(keys[i]));
    }
    if (state.next === null) {
      nums.sort(function (a, b) { return a - b; });
    } else {
      var ref = state.next;
      nums.sort(function (a, b) {
        var da = (a - ref) & 0xFFFF;
        var db = (b - ref) & 0xFFFF;
        return da - db;
      });
    }
    return nums;
  }

  // ---------------------------------------------------------------------------
  // Public API
  // ---------------------------------------------------------------------------

  return {
    // Constants
    MAGIC: MAGIC,
    VERSION: VERSION,
    HEADER_LEN: HEADER_LEN,
    FLAG_FEC: FLAG_FEC,
    FLAG_DTX: FLAG_DTX,
    FLAG_MARKER: FLAG_MARKER,
    AUDIO_PROTOCOL_VERSION: AUDIO_PROTOCOL_VERSION,
    PRESETS: PRESETS,

    // Frame codec
    FrameError: FrameError,
    packFrame: packFrame,
    parseFrame: parseFrame,

    // Stream index + predicates
    buildStreamIndex: buildStreamIndex,
    isOpMic: isOpMic,
    isRxSource: isRxSource,

    // Presets
    presetSubscriptions: presetSubscriptions,

    // Mixer
    defaultMixerEntry: defaultMixerEntry,
    dbToLinear: dbToLinear,
    clampGainDb: clampGainDb,
    effectiveGain: effectiveGain,

    // Uplink coupling
    micWantsUplink: micWantsUplink,
    micLevelFromRms: micLevelFromRms,

    // Seq math
    seqDelta: seqDelta,

    // Jitter buffer
    createJitter: createJitter,
    jitterPush: jitterPush,
    jitterDrain: jitterDrain,
  };
});
