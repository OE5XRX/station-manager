/* OE5XRX Mic AudioWorkletProcessor
   Buffers mic input into 20 ms chunks and posts Float32Array to main thread.
   Runs in the AudioWorklet global scope (no DOM, no window, no WebCodecs).
   Encoding is done on the main thread via AudioEncoder.

   Registered as: "oe5xrx-mic"

   Protocol:
     port.postMessage(Float32Array) — one message per 20 ms chunk (mono).

   The chunk size is derived from the AudioContext sampleRate at construction
   time (sampleRate global provided by the AudioWorklet runtime). */

/* global AudioWorkletProcessor, registerProcessor, sampleRate */

(function () {
  "use strict";

  // Target chunk duration: 20 ms.
  var CHUNK_MS = 20;

  var MicProcessor = (function () {
    // Inherit from AudioWorkletProcessor.
    // In the worklet global, AudioWorkletProcessor is always defined.
    function MicProcessor(options) {
      // super() — call parent constructor.
      AudioWorkletProcessor.call(this, options);

      // Number of mono samples for 20 ms at the context rate.
      this._chunkSize = Math.round((sampleRate * CHUNK_MS) / 1000);
      this._buffer = new Float32Array(this._chunkSize);
      this._writePos = 0;
    }

    // Prototype chain.
    MicProcessor.prototype = Object.create(AudioWorkletProcessor.prototype);
    MicProcessor.prototype.constructor = MicProcessor;

    MicProcessor.prototype.process = function (inputs) {
      // inputs[0] is the first input; inputs[0][0] is channel 0 (mono).
      var input = inputs[0];
      if (!input || !input[0]) {
        // No input data: keep processor alive.
        return true;
      }

      var samples = input[0];

      for (var i = 0; i < samples.length; i++) {
        this._buffer[this._writePos] = samples[i];
        this._writePos += 1;

        if (this._writePos >= this._chunkSize) {
          // Chunk complete — post a copy to the main thread.
          var chunk = new Float32Array(this._buffer);
          this.port.postMessage(chunk, [chunk.buffer]);
          this._writePos = 0;
        }
      }

      // Return true to keep the processor alive.
      return true;
    };

    return MicProcessor;
  }());

  registerProcessor("oe5xrx-mic", MicProcessor);
})();
