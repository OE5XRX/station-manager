/* OE5XRX Audio Panel — live Alpine component.
   Owns the D4 audio WebSocket, WebCodecs decode graph, WebAudio mix, and
   (Task 7) PTT-gated mic uplink with sidetone.

   Pure logic lives in audio-logic.js (window.OE5XRXAudioLogic); this file
   is the stateful shell: connection, message routing, per-stream decode graph,
   jitter buffering, mixer control, subscribe/preset persistence.

   CSP: loaded via <script src> with a nonce; no inline logic. Style mirrors
   static/js/control-panel.js (ES5-ish IIFE + "use strict"; Alpine object uses
   modern method syntax as the component contract requires). */

(function () {
  "use strict";

  var A = window.OE5XRXAudioLogic;

  // Reconnect backoff mirrors control-panel.js exactly.
  var STORAGE_PREFIX = "oe5xrx.audio.";

  // Permanent-deny close codes: same set as control-panel.
  var DENY_CODES = { 4401: true, 4403: true, 4404: true };

  // Level meter: RMS over this many samples of decoded audio.
  var LEVEL_SMOOTHING = 0.8; // IIR coefficient

  // Mic encoder target bitrate (bps).
  var MIC_BITRATE = 16000;

  // 20 ms at 16 kHz mono = 320 samples.
  var MIC_SAMPLES_PER_FRAME = 320;

  // How long to suppress duplicate "not_locked" toasts (ms).
  var NOT_LOCKED_SUPPRESS_MS = 5000;

  // Audio-router virtual module address (station_agent/audio/router_module.py:
  // slot default 1000, MODULE_ID "audio-router", capability "tx_route").
  var ROUTER_SLOT = 1000;
  var ROUTER_MODULE = "audio-router";

  function audioPanel() {
    return {
      // -- reactive state -------------------------------------------------------
      conn: "connecting",       // 'connecting' | 'open' | 'closed'
      supported: false,         // WebCodecs feature-detect
      unsupportedReason: null,  // string if unsupported / terminal error

      streams: [],              // index.list — array of stream entry objects
      subs: {},                 // {[stream_id]: bool}
      mixer: {},                // {[stream_id]: {gainDb, muted}}
      preset: "fm",             // 'fm' | 'satellite' | 'custom'

      micEnabled: false,
      micError: null,           // string if mic could not be enabled
      sidetone: false,
      sidetoneGainDb: -12,

      txRoute: null,            // {slot, module} | null — mirrors store

      levels: {},               // {[stream_id]: 0..1} for meters
      streamState: {},          // {[stream_id]: 'live'|'idle'|'error'} for badges

      // -- non-reactive internals -----------------------------------------------
      _ws: null,
      _retry: 0,
      _closeCode: null,
      _closed: false,
      _stationId: null,
      _listeners: [],

      // Stream index built from "streams" messages.
      _index: { byId: {}, byRef: {}, list: [] },

      // Per-stream WebAudio + decoder state.
      // _streamCtx[stream_id] = {decoder, jitter, gainNode, analyser, playhead}
      _streamCtx: {},

      // AudioContext + master gain.
      _audioCtx: null,
      _masterGain: null,

      // request_id sequence for addressed control commands (tx_route).
      _reqSeq: 0,

      // Mic pipeline (Task 7).
      _micCtx: null,            // dedicated 16 kHz AudioContext for capture/encode
      _micStream: null,         // MediaStream from getUserMedia
      _micSource: null,         // MediaStreamAudioSourceNode (on _micCtx)
      _micWorkletNode: null,    // AudioWorkletNode (oe5xrx-mic, on _micCtx)
      _micEncoder: null,        // AudioEncoder
      _micRate: 16000,          // actual mic context sample rate
      _micSeq: 0,
      _micTs: 0,
      _micPrevKeyed: false,     // to detect unkeyed→keyed edge for mic_open/close
      _sidetoneSource: null,    // MediaStreamAudioSourceNode on _audioCtx (sidetone monitor)
      _sidetoneGain: null,      // GainNode for sidetone path (on _audioCtx)
      _workletLoaded: false,
      _micWarnedNoRef: false,   // one-shot warn when op.mic ref is missing

      // Suppress repeated not_locked toasts.
      _notLockedTimer: null,
      _notLockedShown: false,

      // ---------------------------------------------------------------------
      // init
      // ---------------------------------------------------------------------
      init: function () {
        var root = this.$el;
        this._stationId = root.getAttribute("data-station-id");

        // Guard load order: audio-logic.js must be loaded before this file.
        if (!A) {
          this.supported = false;
          this.unsupportedReason = "audio-logic.js not loaded";
          this.conn = "closed";
          return;
        }

        // WebCodecs feature-detect — before anything else.
        if (
          typeof window.AudioDecoder !== "function" ||
          typeof window.AudioEncoder !== "function"
        ) {
          this.supported = false;
          this.unsupportedReason =
            "WebCodecs is not supported in this browser. " +
            "Please use a recent version of Chrome or Edge.";
          this.conn = "closed";
          return;
        }
        this.supported = true;

        // Load persisted preset + custom subs.
        this._loadState();

        // Build AudioContext + master gain.
        var AudioContext = window.AudioContext || window.webkitAudioContext;
        this._audioCtx = new AudioContext();
        this._masterGain = this._audioCtx.createGain();
        this._masterGain.connect(this._audioCtx.destination);

        this._installListeners();
        this._connect();
      },

      destroy: function () {
        this._teardown();
      },

      // ---------------------------------------------------------------------
      // WebSocket lifecycle — mirrors control-panel.js _wsUrl/_connect/_scheduleReconnect
      // ---------------------------------------------------------------------
      _wsUrl: function () {
        var proto = location.protocol === "https:" ? "wss:" : "ws:";
        return (
          proto + "//" + location.host + "/ws/audio/" + this._stationId + "/"
        );
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
        ws.binaryType = "arraybuffer";
        this._ws = ws;

        ws.addEventListener("open", function () {
          self._retry = 0;
          self.conn = "open";
          // Send hello.
          self._sendJSON({
            v: 1,
            type: "hello",
            codecs: ["opus"],
            webcodecs: true,
          });
          // Re-apply saved subscriptions.
          self._reapplySubscriptions();
        });

        ws.addEventListener("message", function (ev) {
          self._onMessage(ev);
        });

        ws.addEventListener("close", function (ev) {
          self._closeCode = ev.code || null;
          self.conn = "closed";
          self._scheduleReconnect();
        });

        ws.addEventListener("error", function () {
          try {
            ws.close();
          } catch (_) {}
        });
      },

      _scheduleReconnect: function () {
        if (this._closed) return;
        var code = this._closeCode;
        if (DENY_CODES[code]) {
          this.conn = "closed";
          if (!this.unsupportedReason) {
            this.unsupportedReason =
              code === 4401
                ? "You are not signed in."
                : code === 4403
                  ? "You are not permitted to access this station's audio."
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

      _sendJSON: function (obj) {
        if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return false;
        try {
          this._ws.send(JSON.stringify(obj));
          return true;
        } catch (_) {
          return false;
        }
      },

      _sendBinary: function (bytes) {
        if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return false;
        try {
          this._ws.send(bytes);
          return true;
        } catch (_) {
          return false;
        }
      },

      // ---------------------------------------------------------------------
      // Message routing
      // ---------------------------------------------------------------------
      _onMessage: function (ev) {
        // binaryType is "arraybuffer", so media frames arrive as ArrayBuffer.
        if (ev.data instanceof ArrayBuffer) {
          this._onBinaryFrame(ev.data);
          return;
        }
        // JSON text frame.
        var msg;
        try {
          msg = JSON.parse(ev.data);
        } catch (_) {
          return;
        }
        this._routeJSON(msg);
      },

      _routeJSON: function (msg) {
        switch (msg.type) {
          case "streams":
            this._onStreams(msg);
            break;
          case "stream_state":
            this._onStreamState(msg);
            break;
          case "error":
            this._onError(msg);
            break;
          default:
            break; // forward-compat: ignore unknown types
        }
      },

      _onStreams: function (msg) {
        var self = this;
        var prevByRef = this._index.byRef;

        // Rebuild index using audio-logic.
        this._index = A.buildStreamIndex(msg);
        this.streams = this._index.list;

        // Ensure a mixer entry for every stream.
        for (var i = 0; i < this.streams.length; i++) {
          var id = this.streams[i].stream_id;
          if (!this.mixer[id]) {
            this.mixer[id] = A.defaultMixerEntry();
          }
        }

        // Tear down decoders for stream_refs no longer present.
        for (var oldRef in prevByRef) {
          if (!Object.prototype.hasOwnProperty.call(prevByRef, oldRef)) continue;
          if (!this._index.byRef[oldRef]) {
            var oldId = prevByRef[oldRef];
            self._teardownStreamCtx(oldId);
          }
        }

        // Re-send subscribe for any subs we still want (refs may have changed).
        var wantIds = [];
        for (var sid in this.subs) {
          if (Object.prototype.hasOwnProperty.call(this.subs, sid) && this.subs[sid]) {
            wantIds.push(sid);
          }
        }
        if (wantIds.length > 0) {
          this._sendJSON({ v: 1, type: "subscribe", stream_ids: wantIds });
        }
      },

      _onStreamState: function (msg) {
        // Reactive per-stream state map for UI badges (Task 8 renders live/idle/error).
        this.streamState[msg.stream_id] = msg.state || "idle";
      },

      _onError: function (msg) {
        var code = msg.code;
        if (code === "not_locked") {
          // Transient notice, suppress flood.
          if (!this._notLockedShown) {
            this._notLockedShown = true;
            console.warn("[audio] not_locked:", msg.detail);
            var self = this;
            if (this._notLockedTimer) clearTimeout(this._notLockedTimer);
            this._notLockedTimer = setTimeout(function () {
              self._notLockedShown = false;
            }, NOT_LOCKED_SUPPRESS_MS);
          }
        } else {
          console.error("[audio] server error:", code, msg.detail || "");
        }
      },

      // ---------------------------------------------------------------------
      // Binary media frames → jitter → decode
      // ---------------------------------------------------------------------
      _onBinaryFrame: function (buf) {
        var frame;
        try {
          frame = A.parseFrame(buf);
        } catch (_) {
          return;
        }
        // Map stream_ref → stream_id.
        var streamId = this._index.byRef[frame.stream_ref];
        if (!streamId) return; // not in index (yet)

        // Only decode if subscribed.
        if (!this.subs[streamId]) return;

        // Lazily create decoder + graph.
        this._ensureStreamCtx(streamId);

        var ctx = this._streamCtx[streamId];
        if (!ctx) return;

        // Push into jitter buffer.
        A.jitterPush(ctx.jitter, { seq: frame.seq, frame: frame });

        // Drain and decode/PLC each ready item.
        var result = A.jitterDrain(ctx.jitter);
        for (var i = 0; i < result.out.length; i++) {
          var item = result.out[i];
          if (item.plc) {
            // PLC: skip chunk — Opus decoder handles concealment inherently.
            continue;
          }
          this._decodeChunk(streamId, ctx, item.frame);
        }
      },

      // ---------------------------------------------------------------------
      // Per-stream decode graph
      // ---------------------------------------------------------------------
      _ensureStreamCtx: function (streamId) {
        if (this._streamCtx[streamId]) return;
        var entry = this._index.byId[streamId];
        if (!entry) return;

        var self = this;
        var audioCtx = this._audioCtx;

        // GainNode for this stream → master.
        var gainNode = audioCtx.createGain();
        var mixerEntry = this.mixer[streamId] || A.defaultMixerEntry();
        gainNode.gain.value = A.effectiveGain(mixerEntry);
        gainNode.connect(this._masterGain);

        // AnalyserNode for RMS level metering.
        var analyser = audioCtx.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = LEVEL_SMOOTHING;
        gainNode.connect(analyser);

        var sampleRate = entry.format ? entry.format.rate : 8000;
        var channels = entry.format ? entry.format.channels : 1;

        // AudioDecoder — one per stream.
        var decoder = new window.AudioDecoder({
          output: function (audioData) {
            self._onDecodedAudio(streamId, audioData);
          },
          error: function (e) {
            console.error("[audio] decoder error for", streamId, e);
          },
        });

        try {
          decoder.configure({
            codec: "opus",
            sampleRate: sampleRate,
            numberOfChannels: channels,
          });
        } catch (e) {
          console.error("[audio] decoder configure failed for", streamId, e);
          decoder.close();
          gainNode.disconnect();
          analyser.disconnect();
          return;
        }

        // Playhead: scheduled time for next chunk.
        this._streamCtx[streamId] = {
          decoder: decoder,
          jitter: A.createJitter({ depth: 3 }),
          gainNode: gainNode,
          analyser: analyser,
          playhead: audioCtx.currentTime + 0.05, // 50 ms initial buffer
          sampleRate: sampleRate,
          channels: channels,
          levelBuf: null, // allocated lazily on first analyser read
        };
      },

      _decodeChunk: function (streamId, ctx, frame) {
        if (!ctx || !ctx.decoder || ctx.decoder.state === "closed") return;
        try {
          var chunk = new window.EncodedAudioChunk({
            type: "key",
            // Timestamp in microseconds (WebCodecs convention).
            timestamp: Math.round(frame.ts * (1e6 / ctx.sampleRate)),
            data: frame.payload,
          });
          ctx.decoder.decode(chunk);
        } catch (e) {
          console.warn("[audio] decode error for", streamId, e);
        }
      },

      _onDecodedAudio: function (streamId, audioData) {
        var ctx = this._streamCtx[streamId];
        if (!ctx) {
          audioData.close();
          return;
        }
        var audioCtx = this._audioCtx;

        // Resume context if suspended (autoplay policy).
        if (audioCtx.state === "suspended") {
          audioCtx.resume().catch(function () {});
        }

        var numChannels = audioData.numberOfChannels;
        var numFrames = audioData.numberOfFrames;
        var sampleRate = audioData.sampleRate;

        // Copy AudioData into an AudioBuffer for scheduling.
        var buffer = audioCtx.createBuffer(numChannels, numFrames, sampleRate);
        for (var ch = 0; ch < numChannels; ch++) {
          var channelData = buffer.getChannelData(ch);
          audioData.copyTo(channelData, { planeIndex: ch });
        }
        audioData.close();

        // Update level meter (cheap RMS on channel 0).
        var samples = buffer.getChannelData(0);
        var rms = 0;
        for (var i = 0; i < samples.length; i++) {
          rms += samples[i] * samples[i];
        }
        rms = Math.sqrt(rms / (samples.length || 1));
        // Clamp to 0..1.
        this.levels[streamId] = Math.min(1, rms * 2);

        // Schedule for gapless playback.
        var now = audioCtx.currentTime;
        if (ctx.playhead < now) {
          // We fell behind — reset to now + small buffer.
          ctx.playhead = now + 0.02;
        }
        var source = audioCtx.createBufferSource();
        source.buffer = buffer;
        source.connect(ctx.gainNode);
        // Drop the node once it finishes so stale sources don't accumulate
        // during rapid subscribe/unsubscribe.
        source.onended = function () {
          try {
            source.disconnect();
          } catch (e) {}
        };
        source.start(ctx.playhead);
        ctx.playhead += buffer.duration;
      },

      _teardownStreamCtx: function (streamId) {
        var ctx = this._streamCtx[streamId];
        if (!ctx) return;
        try {
          if (ctx.decoder && ctx.decoder.state !== "closed") ctx.decoder.close();
        } catch (_) {}
        try {
          ctx.gainNode.disconnect();
        } catch (_) {}
        try {
          ctx.analyser.disconnect();
        } catch (_) {}
        delete this._streamCtx[streamId];
        delete this.levels[streamId];
      },

      // ---------------------------------------------------------------------
      // Subscribe / preset control
      // ---------------------------------------------------------------------
      isSubscribed: function (streamId) {
        return !!this.subs[streamId];
      },

      toggleSub: function (streamId) {
        if (this.subs[streamId]) {
          this.subs[streamId] = false;
          this._sendJSON({ v: 1, type: "unsubscribe", stream_ids: [streamId] });
          this._teardownStreamCtx(streamId);
        } else {
          this.subs[streamId] = true;
          this._sendJSON({ v: 1, type: "subscribe", stream_ids: [streamId] });
        }
        // Any manual toggle makes this "custom" unless it already matches a preset.
        this.preset = "custom";
        this._saveState();
      },

      applyPreset: function (name) {
        this.preset = name;
        var want = A.presetSubscriptions(name, this.streams);

        // Build sets.
        var wantSet = {};
        for (var wi = 0; wi < want.length; wi++) {
          wantSet[want[wi]] = true;
        }

        // Subscribe additions.
        var toSub = [];
        for (var si = 0; si < want.length; si++) {
          if (!this.subs[want[si]]) {
            this.subs[want[si]] = true;
            toSub.push(want[si]);
          }
        }
        if (toSub.length > 0) {
          this._sendJSON({ v: 1, type: "subscribe", stream_ids: toSub });
        }

        // Unsubscribe removals.
        var toUnsub = [];
        for (var uid in this.subs) {
          if (!Object.prototype.hasOwnProperty.call(this.subs, uid)) continue;
          if (this.subs[uid] && !wantSet[uid]) {
            this.subs[uid] = false;
            toUnsub.push(uid);
            this._teardownStreamCtx(uid);
          }
        }
        if (toUnsub.length > 0) {
          this._sendJSON({ v: 1, type: "unsubscribe", stream_ids: toUnsub });
        }

        this._saveState();
      },

      // Re-send subscriptions (e.g. after reconnect / streams refresh).
      _reapplySubscriptions: function () {
        var ids = [];
        for (var sid in this.subs) {
          if (Object.prototype.hasOwnProperty.call(this.subs, sid) && this.subs[sid]) {
            ids.push(sid);
          }
        }
        if (ids.length > 0) {
          this._sendJSON({ v: 1, type: "subscribe", stream_ids: ids });
        }
      },

      // ---------------------------------------------------------------------
      // Mixer
      // ---------------------------------------------------------------------
      setGain: function (streamId, raw) {
        if (!this.mixer[streamId]) this.mixer[streamId] = A.defaultMixerEntry();
        this.mixer[streamId].gainDb = A.clampGainDb(raw);
        this._applyGain(streamId);
      },

      toggleMute: function (streamId) {
        if (!this.mixer[streamId]) this.mixer[streamId] = A.defaultMixerEntry();
        this.mixer[streamId].muted = !this.mixer[streamId].muted;
        this._applyGain(streamId);
      },

      _applyGain: function (streamId) {
        var ctx = this._streamCtx[streamId];
        if (!ctx) return;
        var entry = this.mixer[streamId];
        ctx.gainNode.gain.value = A.effectiveGain(entry);
      },

      // ---------------------------------------------------------------------
      // txRoute — addressed to the audio-router virtual module; the TX target
      // (slot/module args) goes ONLY into `value`. A null slot clears the route.
      // Delegates to the control store's sendCommand (lock-holding control WS).
      // ---------------------------------------------------------------------
      setTxRoute: function (slot, module) {
        var value =
          slot === null || slot === undefined
            ? null
            : { slot: slot, module: module };
        this.txRoute = value;
        var store = window.Alpine.store && window.Alpine.store("control");
        if (store) {
          this._reqSeq += 1;
          store.sendCommand({
            type: "command",
            request_id: "audio-tx-" + this._reqSeq + "-" + Date.now(),
            slot: ROUTER_SLOT,
            module: ROUTER_MODULE,
            capability: "tx_route",
            op: "set",
            value: value,
          });
        }
      },

      // ---------------------------------------------------------------------
      // Persistence
      // ---------------------------------------------------------------------
      _storageKey: function () {
        return STORAGE_PREFIX + this._stationId;
      },

      _saveState: function () {
        try {
          var data = {
            preset: this.preset,
            subs: this.subs,
            mixer: this.mixer,
          };
          localStorage.setItem(this._storageKey(), JSON.stringify(data));
        } catch (_) {}
      },

      _loadState: function () {
        try {
          var raw = localStorage.getItem(this._storageKey());
          if (!raw) return;
          var data = JSON.parse(raw);
          if (data.preset) this.preset = data.preset;
          if (data.subs && typeof data.subs === "object") this.subs = data.subs;
          if (data.mixer && typeof data.mixer === "object") this.mixer = data.mixer;
        } catch (_) {}
      },

      // ---------------------------------------------------------------------
      // Global listeners + teardown
      // ---------------------------------------------------------------------
      _installListeners: function () {
        var self = this;
        var onBeforeUnload = function () {
          self._teardown();
        };
        this._addListener(window, "beforeunload", onBeforeUnload);
      },

      _addListener: function (target, type, handler) {
        target.addEventListener(type, handler);
        this._listeners.push({ target: target, type: type, handler: handler });
      },

      _teardown: function () {
        if (this._closed) return;
        this._closed = true;

        // Mic cleanup.
        this._disableMicInternal(false /* don't send mic_close if WS already closing */);

        // Tear down all stream decoders.
        for (var sid in this._streamCtx) {
          if (Object.prototype.hasOwnProperty.call(this._streamCtx, sid)) {
            this._teardownStreamCtx(sid);
          }
        }

        // Close AudioContext.
        if (this._audioCtx) {
          try {
            this._audioCtx.close();
          } catch (_) {}
          this._audioCtx = null;
        }

        // Remove listeners.
        for (var i = 0; i < this._listeners.length; i++) {
          var l = this._listeners[i];
          try {
            l.target.removeEventListener(l.type, l.handler);
          } catch (_) {}
        }
        this._listeners = [];

        // Close WS.
        if (this._ws) {
          try {
            this._ws.close();
          } catch (_) {}
          this._ws = null;
        }

        this.streamState = {};

        if (this._notLockedTimer) clearTimeout(this._notLockedTimer);
      },

      // =========================================================================
      // Task 7: Mic capture → encode → PTT-gated uplink + sidetone
      // =========================================================================

      // ---------------------------------------------------------------------
      // enableMic — getUserMedia + worklet + AudioEncoder
      // ---------------------------------------------------------------------
      enableMic: function () {
        if (this.micEnabled) return;
        var self = this;
        this.micError = null;

        // The worklet is REQUIRED. Its URL comes from data-worklet-url on the
        // panel root element (never hardcoded).
        var workletUrl = this.$el
          ? this.$el.getAttribute("data-worklet-url")
          : null;
        if (!workletUrl) {
          this.micError = "Microphone unavailable: audio worklet failed to load";
          return;
        }

        // Dedicated 16 kHz capture context — WebCodecs AudioEncoder does NOT
        // resample, so the mic must be captured at the encoder's rate.
        var AudioContext = window.AudioContext || window.webkitAudioContext;
        var micCtx;
        try {
          micCtx = new AudioContext({ sampleRate: 16000 });
        } catch (_) {
          // Fall back to the main context only if the constructor throws.
          micCtx = this._audioCtx;
        }
        if (!micCtx) {
          this.micError = "Microphone unavailable: no audio context";
          return;
        }
        if (micCtx.sampleRate !== 16000) {
          this.micError =
            "Microphone unavailable: 16 kHz capture unsupported (got " +
            micCtx.sampleRate +
            " Hz)";
          if (micCtx !== this._audioCtx) {
            try {
              micCtx.close();
            } catch (_) {}
          }
          return;
        }
        this._micCtx = micCtx;
        this._micRate = micCtx.sampleRate;

        // Load the worklet on the mic context; on failure, bail cleanly.
        micCtx.audioWorklet
          .addModule(workletUrl)
          .then(function () {
            self._workletLoaded = true;
            return navigator.mediaDevices.getUserMedia({
              audio: {
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
              },
            });
          })
          .then(function (stream) {
            // A concurrent disableMic()/teardown may have cleared _micCtx.
            if (self._micCtx !== micCtx) {
              stream.getTracks().forEach(function (t) {
                t.stop();
              });
              return null;
            }
            self._micStream = stream;
            self._micSource = micCtx.createMediaStreamSource(stream);

            self._micWorkletNode = new window.AudioWorkletNode(
              micCtx,
              "oe5xrx-mic"
            );
            self._micSource.connect(self._micWorkletNode);
            self._micWorkletNode.port.onmessage = function (ev) {
              self._onMicChunk(ev.data);
            };

            // Resolve the encoder config ONCE before creating the encoder:
            // prefer VOIP + in-band FEC if supported, else baseline.
            var baseConfig = {
              codec: "opus",
              sampleRate: self._micRate,
              numberOfChannels: 1,
              bitrate: MIC_BITRATE,
            };
            var voipConfig = Object.assign({}, baseConfig, {
              opus: { application: "voip", useinbandfec: true },
            });
            if (
              window.AudioEncoder &&
              typeof window.AudioEncoder.isConfigSupported === "function"
            ) {
              return window.AudioEncoder.isConfigSupported(voipConfig).then(
                function (support) {
                  return support && support.supported ? voipConfig : baseConfig;
                },
                function () {
                  return baseConfig;
                }
              );
            }
            return baseConfig;
          })
          .then(function (config) {
            if (!config) return; // superseded by teardown above
            // Guard against a concurrent disableMic().
            if (self._micCtx !== micCtx || !self._micSource) return;

            self._micSeq = 0;
            self._micTs = 0;
            self._micPrevKeyed = false;

            var encoder = new window.AudioEncoder({
              output: function (chunk) {
                if (!self._micEncoder) return; // superseded by disableMic
                self._onEncodedMic(chunk);
              },
              error: function (e) {
                console.error("[audio] mic encoder error", e);
              },
            });
            encoder.configure(config);
            self._micEncoder = encoder;

            self.micEnabled = true;

            // Re-apply sidetone routing now that _micSource exists.
            if (self.sidetone) self._updateSidetone();
          })
          .catch(function (e) {
            console.error("[audio] enableMic failed:", e);
            self.micError =
              "Microphone unavailable: audio worklet failed to load";
            // Stop any track we already opened and drop the mic context.
            self._disableMicInternal(false);
          });
      },

      // ---------------------------------------------------------------------
      // Mic chunk from worklet → encode
      // ---------------------------------------------------------------------
      _onMicChunk: function (float32Chunk) {
        if (!this._micEncoder || !this.micEnabled) return;

        // Build an AudioData for the encoder at the mic context's actual rate.
        try {
          var data = new window.AudioData({
            format: "f32",
            sampleRate: this._micRate,
            numberOfChannels: 1,
            numberOfFrames: float32Chunk.length,
            timestamp: Math.round((this._micTs / this._micRate) * 1e6),
            data: float32Chunk,
          });
          this._micEncoder.encode(data);
          data.close();
        } catch (e) {
          console.warn("[audio] mic encode error:", e);
        }
      },

      // ---------------------------------------------------------------------
      // Encoded mic output — PTT-gated WS send
      // ---------------------------------------------------------------------
      _onEncodedMic: function (chunk) {
        if (!this.micEnabled) return;
        var store = window.Alpine.store && window.Alpine.store("control");
        if (!store) return;

        var wantsUplink = A.micWantsUplink({
          micEnabled: this.micEnabled,
          keyed: store.keyed,
          youHold: store.youHold,
        });

        // Detect keying edge transitions.
        var keyed = !!(store.keyed && store.youHold);
        if (keyed && !this._micPrevKeyed) {
          // UNKEYED→KEYED edge: send mic_open once.
          this._sendJSON({
            v: 1,
            type: "mic_open",
            format: { rate: 16000, channels: 1 },
            codec: "opus",
          });
        } else if (!keyed && this._micPrevKeyed) {
          // KEYED→UNKEYED edge: send mic_close once.
          this._sendJSON({ v: 1, type: "mic_close" });
        }
        this._micPrevKeyed = keyed;

        if (!wantsUplink) {
          // Advance ts to keep encoder clock running even when not transmitting.
          this._micTs += MIC_SAMPLES_PER_FRAME;
          return;
        }

        // Resolve the op.mic stream_ref from the index. If absent, DROP the
        // frame — never send with a fabricated stream_ref.
        var micStreamId = this._findMicStreamId();
        var micEntry = micStreamId ? this._index.byId[micStreamId] : null;
        if (!micEntry || micEntry.stream_ref === undefined || micEntry.stream_ref === null) {
          if (!this._micWarnedNoRef) {
            this._micWarnedNoRef = true;
            console.warn("[audio] op.mic stream_ref not in index — dropping uplink frames");
          }
          this._micTs += MIC_SAMPLES_PER_FRAME;
          return;
        }
        this._micWarnedNoRef = false;
        var micRef = micEntry.stream_ref;

        // Copy encoded bytes out of the chunk.
        var payload = new Uint8Array(chunk.byteLength);
        chunk.copyTo(payload);

        var frame = A.packFrame({
          stream_ref: micRef,
          seq: this._micSeq & 0xffff,
          ts: this._micTs >>> 0,
          flags: 0,
          payload: payload,
        });

        this._micSeq = (this._micSeq + 1) & 0xffff;
        this._micTs += MIC_SAMPLES_PER_FRAME;

        this._sendBinary(frame.buffer);
      },

      _findMicStreamId: function () {
        for (var i = 0; i < this.streams.length; i++) {
          if (A.isOpMic(this.streams[i])) return this.streams[i].stream_id;
        }
        return null;
      },

      // ---------------------------------------------------------------------
      // disableMic
      // ---------------------------------------------------------------------
      disableMic: function () {
        this._disableMicInternal(true);
      },

      _disableMicInternal: function (sendClose) {
        if (
          !this.micEnabled &&
          !this._micStream &&
          !this._micCtx &&
          !this._micEncoder
        ) {
          return;
        }

        if (sendClose) {
          this._sendJSON({ v: 1, type: "mic_close" });
        }

        // Sidetone nodes live on the main playback context — disconnect + drop
        // them so they do not survive an enable/disable cycle.
        if (this._sidetoneGain) {
          try {
            this._sidetoneGain.disconnect();
          } catch (_) {}
          this._sidetoneGain = null;
        }
        if (this._sidetoneSource) {
          try {
            this._sidetoneSource.disconnect();
          } catch (_) {}
          this._sidetoneSource = null;
        }

        if (this._micWorkletNode) {
          try {
            this._micWorkletNode.disconnect();
          } catch (_) {}
          this._micWorkletNode = null;
        }

        if (this._micEncoder) {
          try {
            if (this._micEncoder.state !== "closed") this._micEncoder.close();
          } catch (_) {}
          this._micEncoder = null;
        }

        if (this._micSource) {
          try {
            this._micSource.disconnect();
          } catch (_) {}
          this._micSource = null;
        }

        if (this._micStream) {
          try {
            this._micStream.getTracks().forEach(function (t) {
              t.stop();
            });
          } catch (_) {}
          this._micStream = null;
        }

        // Close the dedicated mic context (unless it was the shared main ctx).
        if (this._micCtx && this._micCtx !== this._audioCtx) {
          try {
            this._micCtx.close();
          } catch (_) {}
        }
        this._micCtx = null;

        this.micEnabled = false;
        this._micPrevKeyed = false;
        this._micWarnedNoRef = false;
      },

      // ---------------------------------------------------------------------
      // Sidetone
      // ---------------------------------------------------------------------
      toggleSidetone: function () {
        this.sidetone = !this.sidetone;
        this._updateSidetone();
      },

      _updateSidetone: function () {
        // Sidetone is a browser-local monitor path. The mic ENCODE path lives on
        // _micCtx (16 kHz capture context). Web Audio nodes cannot connect across
        // contexts, so the sidetone monitor is built on the MAIN _audioCtx using
        // a separate MediaStreamAudioSourceNode fed by the same _micStream
        // (sharing a MediaStream across contexts is allowed; sharing nodes is not).
        if (!this._micStream || !this._audioCtx) return;

        if (!this._sidetoneGain) {
          // Build the sidetone graph on the main playback context.
          this._sidetoneSource = this._audioCtx.createMediaStreamSource(
            this._micStream
          );
          this._sidetoneGain = this._audioCtx.createGain();
          this._sidetoneGain.gain.value = A.dbToLinear(
            A.clampGainDb(this.sidetoneGainDb)
          );
          // Wire: sidetoneSource → sidetoneGain → _masterGain (reaches speakers).
          this._sidetoneSource.connect(this._sidetoneGain);
          this._sidetoneGain.connect(this._masterGain);
        } else {
          this._sidetoneGain.gain.value = A.dbToLinear(
            A.clampGainDb(this.sidetoneGainDb)
          );
        }

        if (!this.sidetone) {
          // Sidetone toggled off — disconnect and drop both nodes.
          try {
            this._sidetoneGain.disconnect();
          } catch (_) {}
          this._sidetoneGain = null;
          try {
            this._sidetoneSource.disconnect();
          } catch (_) {}
          this._sidetoneSource = null;
        }
      },
    };
  }

  document.addEventListener("alpine:init", function () {
    window.Alpine.data("audioPanel", audioPanel);
  });
})();
