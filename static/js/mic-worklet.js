/* OE5XRX Mic AudioWorkletProcessor
   Buffers mic input into 20 ms chunks and posts Float32Array to main thread.
   Runs in the AudioWorklet global scope (no DOM, no window, no WebCodecs).
   Encoding is done on the main thread via AudioEncoder.

   Registered as: "oe5xrx-mic"

   Protocol:
     port.postMessage(Float32Array) — one message per 20 ms chunk (mono).

   The chunk size is derived from the AudioContext sampleRate at construction
   time (sampleRate global provided by the AudioWorklet runtime).

   IMPORTANT: AudioWorkletProcessor is a real ES class. It MUST be subclassed
   with `class ... extends AudioWorkletProcessor` and `super()`. ES5-style
   pseudo-inheritance (`AudioWorkletProcessor.call(this)`) throws
   "Class constructor cannot be invoked without 'new'" on the audio thread,
   the processor never instantiates, and process() is never called — the mic
   silently produces zero frames. The AudioWorklet global scope is always
   modern (ES2017+), so a native class is safe here. */

/* global AudioWorkletProcessor, registerProcessor, sampleRate */

"use strict";

// Target chunk duration: 20 ms.
const CHUNK_MS = 20;

class MicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super(options);

    // Number of mono samples for 20 ms at the context rate.
    this._chunkSize = Math.round((sampleRate * CHUNK_MS) / 1000);
    this._buffer = new Float32Array(this._chunkSize);
    this._writePos = 0;

    // Windowed RMS/peak accumulator (T1 tap — worklet output path).
    this._diagSumSq = 0;
    this._diagPeak = 0;
    this._diagCount = 0;

    // Oscillator inject state: synthesize a sine instead of mic when active.
    this._injectOn = false;
    this._injectFreq = 1000;   // Hz
    this._injectAmplitude = 0; // linear 0..1
    this._injectPhase = 0;     // radians, continuous across chunks

    // Handle control messages from the main thread.
    this.port.onmessage = (ev) => {
      const msg = ev.data;
      if (!msg || !msg.type) return;

      if (msg.type === "diag_report") {
        // Compute windowed RMS and peak of worklet output (T1 tap).
        const rms = this._diagCount > 0
          ? Math.sqrt(this._diagSumSq / this._diagCount)
          : 0;
        const peak = this._diagPeak;
        this.port.postMessage({ type: "diag_tap", point: "T1", rms, peak });

      } else if (msg.type === "diag_inject") {
        // Toggle oscillator inject. When on, synthesize sine instead of mic.
        this._injectOn = !!msg.on;
        if (msg.freq !== undefined) this._injectFreq = msg.freq;
        // levelDbfs: convert dBFS to linear amplitude; default -12 dBFS.
        const dbfs = (msg.levelDbfs !== undefined) ? msg.levelDbfs : -12;
        this._injectAmplitude = Math.pow(10, dbfs / 20);
      }
    };
  }

  process(inputs, outputs) {
    // inputs[0] is the first input; inputs[0][0] is channel 0 (mono).
    const input = inputs[0];

    if (this._injectOn) {
      // Synthesize sine tone, ignoring mic input. Use the first output channel
      // if available so downstream nodes can also hear the tone; for the chunk
      // path we write directly into this._buffer.
      const out = outputs && outputs[0] && outputs[0][0];
      const twoPiFreq = 2 * Math.PI * this._injectFreq / sampleRate;
      for (let i = 0; i < (out ? out.length : 128); i++) {
        const s = this._injectAmplitude * Math.sin(this._injectPhase);
        this._injectPhase += twoPiFreq;
        if (this._injectPhase > 2 * Math.PI) this._injectPhase -= 2 * Math.PI;

        this._buffer[this._writePos] = s;
        this._writePos += 1;

        // Accumulate diagnostics on injected output.
        this._diagSumSq += s * s;
        const abs = s < 0 ? -s : s;
        if (abs > this._diagPeak) this._diagPeak = abs;
        this._diagCount += 1;

        if (this._writePos >= this._chunkSize) {
          const chunk = new Float32Array(this._buffer);
          this.port.postMessage(chunk, [chunk.buffer]);
          this._writePos = 0;
        }
      }
      return true;
    }

    if (!input || !input[0]) {
      // No input data yet: keep the processor alive.
      return true;
    }

    const samples = input[0];

    for (let i = 0; i < samples.length; i++) {
      const s = samples[i];
      this._buffer[this._writePos] = s;
      this._writePos += 1;

      // Accumulate windowed diagnostics on actual mic output.
      this._diagSumSq += s * s;
      const abs = s < 0 ? -s : s;
      if (abs > this._diagPeak) this._diagPeak = abs;
      this._diagCount += 1;

      if (this._writePos >= this._chunkSize) {
        // Chunk complete — post a copy to the main thread (transfer the buffer).
        const chunk = new Float32Array(this._buffer);
        this.port.postMessage(chunk, [chunk.buffer]);
        this._writePos = 0;
      }
    }

    // Return true to keep the processor alive.
    return true;
  }
}

registerProcessor("oe5xrx-mic", MicProcessor);
