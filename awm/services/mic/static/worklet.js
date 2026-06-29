// AudioWorklet: converts mic Float32 frames to s16le PCM and posts them to the
// main thread, which forwards them over the WebSocket.
class PCMWorklet extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      const ch = input[0]; // mono channel, Float32 in [-1, 1]
      const pcm = new Int16Array(ch.length);
      for (let i = 0; i < ch.length; i++) {
        let s = Math.max(-1, Math.min(1, ch[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true; // keep processor alive
  }
}
registerProcessor("pcm-worklet", PCMWorklet);
