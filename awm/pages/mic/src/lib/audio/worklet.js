// AudioWorklet: mic Float32 → s16le PCM, posted to the main thread in ~20 ms
// frames. No resampling — the capture graph runs at the device's native rate
// and the service is told that rate when the session opens, so `pacat` plays it
// back at exactly the rate it was captured at.
//
// The 20 ms framing is the one thing this does beyond format conversion, and it
// is not cosmetic: a worklet quantum is 128 samples (~2.7 ms at 48 kHz), so
// posting per quantum would put ~375 messages/second on a WebSocket that is now
// relayed through the gateway's shared event loop. At 20 ms that is 50.
class PCMWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frameSamples = Math.round(sampleRate * 0.02);
    this.buf = new Int16Array(this.frameSamples);
    this.n = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true; // keep processor alive
    const ch = input[0]; // mono channel, Float32 in [-1, 1]
    for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]));
      this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n === this.frameSamples) {
        const out = this.buf;
        this.port.postMessage(out.buffer, [out.buffer]);
        this.buf = new Int16Array(this.frameSamples); // the old one was transferred
        this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor('pcm-worklet', PCMWorklet);
