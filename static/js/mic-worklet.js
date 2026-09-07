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
  }

  process(inputs) {
    // inputs[0] is the first input; inputs[0][0] is channel 0 (mono).
    const input = inputs[0];
    if (!input || !input[0]) {
      // No input data yet: keep the processor alive.
      return true;
    }

    const samples = input[0];

    for (let i = 0; i < samples.length; i++) {
      this._buffer[this._writePos] = samples[i];
      this._writePos += 1;

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
