// AudioWorklet that downsamples the mic to 16kHz mono int16 PCM and
// posts ~20ms frames back to the main thread.
//
// The browser's AudioContext usually runs at 44.1k or 48k; we sample
// every Nth frame to approximate 16k. Good enough for whisper.

class MicDownsampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = 16000;
    this.frameSamplesAtTarget = 320; // 20ms @ 16k
    this.acc = [];
    this.accCount = 0;
    this.step = sampleRate / this.targetRate;
    this.cursor = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch0 = input[0]; // mono
    if (!ch0) return true;
    for (let i = 0; i < ch0.length; i++) {
      this.cursor += 1;
      if (this.cursor >= this.step) {
        this.cursor -= this.step;
        // Take the current sample (nearest-neighbor; good enough for STT)
        const s = Math.max(-1, Math.min(1, ch0[i]));
        const int16 = s < 0 ? s * 0x8000 : s * 0x7fff;
        this.acc.push(int16);
        this.accCount += 1;
        if (this.accCount >= this.frameSamplesAtTarget) {
          const buf = new Int16Array(this.acc);
          this.port.postMessage(buf.buffer, [buf.buffer]);
          this.acc = [];
          this.accCount = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("mic-downsampler", MicDownsampler);
