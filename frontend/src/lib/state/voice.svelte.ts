/**
 * Voice/PTT state — mic capture, WS streaming, STT auto-send.
 *
 * Ported from dev/awm/static/voice-panel.js (276-line IIFE) into a rune-based
 * singleton so any component can `import { voice }` and react. The on-screen
 * piece (VoicePanel.svelte) is intentionally thin: it renders the status badge
 * and VU meter from this store. PttButton drives start()/end()/cancel().
 *
 * Connection model:
 *   - One WebSocket to /voice/ws (cookie auth, no subprotocol needed). Auto-
 *     reconnect with 1s→15s exponential backoff.
 *   - When WS is OPEN: PCM frames go up live, server runs STT on `{type:end}`
 *     and broadcasts `{type:stt_result, text}` to every tab of the same user.
 *   - When WS is closed: we buffer PCM locally and POST it to /voice/stt on
 *     PTT release. Single tab gets the result. 1 MiB cap (~32s @ 16k mono).
 *
 * Result fan-out:
 *   - Subscribers register via onResult(cb). The focus page uses this to
 *     inject the transcription into the composer and auto-send.
 */

import { base } from '$app/paths';
import { wsUrl } from '../api/config';

export type VoiceConnection = 'disconnected' | 'connecting' | 'connected';
export type VoiceStage = 'idle' | 'recording' | 'transcribing' | 'error';

type ResultListener = (text: string) => void;

const WORKLET_PATH = `${base}/mic-worklet.js`;
const STT_HTTP_PATH = '/voice/stt';
const MAX_PCM_BYTES = 1024 * 1024; // ~32s @ 16k mono int16

class VoiceState {
  connection = $state<VoiceConnection>('disconnected');
  stage      = $state<VoiceStage>('idle');
  stageText  = $state<string>('idle');
  meter      = $state<number>(0);            // 0–1 RMS for the VU bar
  lastError  = $state<string>('');

  private ws: WebSocket | null = null;
  private wsBackoff = 1000;
  private wsWant = false;

  private audioCtx: AudioContext | null = null;
  private micStream: MediaStream | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private analyser: AnalyserNode | null = null;
  private analyserBuf: Float32Array | null = null;
  private meterRaf: number | null = null;
  private micConnected = false;

  private recording = false;
  private pcmBuf: Uint8Array[] = [];
  private pcmBytes = 0;

  private listeners = new Set<ResultListener>();

  // --- subscription -----------------------------------------------------

  onResult(fn: ResultListener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private fireResult(text: string): void {
    for (const fn of this.listeners) {
      try { fn(text); } catch (err) { console.error('[voice listener]', err); }
    }
  }

  // --- lifecycle --------------------------------------------------------

  /** Begin connecting the WS. Idempotent; safe to call from onMount. */
  connect(): void {
    if (this.wsWant) return;
    this.wsWant = true;
    this.openWs();
  }

  /** Close WS and release the mic. Call from onDestroy. */
  disconnect(): void {
    this.wsWant = false;
    if (this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
    }
    this.releaseMic();
    this.connection = 'disconnected';
  }

  private openWs(): void {
    this.connection = 'connecting';
    const sock = new WebSocket(wsUrl('/voice/ws'));
    sock.binaryType = 'arraybuffer';
    this.ws = sock;

    sock.onopen = () => {
      this.connection = 'connected';
      this.wsBackoff = 1000;
    };
    sock.onclose = () => {
      this.connection = 'disconnected';
      if (this.wsWant) {
        const delay = this.wsBackoff;
        this.wsBackoff = Math.min(this.wsBackoff * 2, 15000);
        setTimeout(() => { if (this.wsWant) this.openWs(); }, delay);
      }
    };
    sock.onerror = () => {
      // onclose follows; just log the state.
      this.lastError = 'ws error';
    };
    sock.onmessage = (e) => {
      if (typeof e.data !== 'string') return;
      let msg: { type?: string; text?: string; stage?: string; message?: string };
      try { msg = JSON.parse(e.data); } catch { return; }
      switch (msg.type) {
        case 'ready':
          this.setStage('idle', 'idle');
          break;
        case 'status':
          this.setStage((msg.stage as VoiceStage) ?? 'idle', msg.text || msg.stage || '');
          break;
        case 'stt_result':
          if (msg.text) this.fireResult(msg.text);
          break;
        case 'error':
          this.setStage('error', msg.message || 'error');
          this.lastError = msg.message || 'error';
          break;
      }
    };
  }

  // --- mic capture ------------------------------------------------------

  private async ensureAudio(): Promise<void> {
    if (this.audioCtx) return;
    const Ctx = (window.AudioContext || (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext);
    if (!Ctx) throw new Error('no AudioContext');
    this.audioCtx = new Ctx();
    await this.audioCtx.audioWorklet.addModule(WORKLET_PATH);
  }

  private async startMicCapture(): Promise<void> {
    if (this.micConnected) return;
    await this.ensureAudio();
    const ctx = this.audioCtx!;
    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    const src = ctx.createMediaStreamSource(this.micStream);
    this.workletNode = new AudioWorkletNode(ctx, 'mic-downsampler');
    this.workletNode.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
      if (!this.recording) return;
      const data = e.data;
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try { this.ws.send(data); } catch { /* ignore */ }
      } else if (this.pcmBytes + data.byteLength <= MAX_PCM_BYTES) {
        this.pcmBuf.push(new Uint8Array(data));
        this.pcmBytes += data.byteLength;
      }
    };
    src.connect(this.workletNode);
    // Worklet must reach the destination for the process() loop to run; a
    // gain=0 node keeps it silent.
    const silent = ctx.createGain();
    silent.gain.value = 0;
    this.workletNode.connect(silent).connect(ctx.destination);

    // Analyser for the VU meter — drives `meter` 0..1.
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    src.connect(this.analyser);
    this.analyserBuf = new Float32Array(this.analyser.fftSize);
    const tick = () => {
      if (!this.analyser || !this.analyserBuf) return;
      this.analyser.getFloatTimeDomainData(this.analyserBuf);
      let sum = 0;
      for (let i = 0; i < this.analyserBuf.length; i++) {
        sum += this.analyserBuf[i] * this.analyserBuf[i];
      }
      const rms = Math.sqrt(sum / this.analyserBuf.length);
      this.meter = Math.min(1, rms * 6); // scale so normal speech ~0.5
      this.meterRaf = requestAnimationFrame(tick);
    };
    tick();
    this.micConnected = true;
  }

  private releaseMic(): void {
    if (this.meterRaf !== null) {
      cancelAnimationFrame(this.meterRaf);
      this.meterRaf = null;
    }
    if (this.micStream) {
      for (const t of this.micStream.getTracks()) {
        try { t.stop(); } catch { /* ignore */ }
      }
      this.micStream = null;
    }
    if (this.workletNode) {
      try { this.workletNode.disconnect(); } catch { /* ignore */ }
      this.workletNode = null;
    }
    this.analyser = null;
    this.analyserBuf = null;
    if (this.audioCtx) {
      try { this.audioCtx.close(); } catch { /* ignore */ }
      this.audioCtx = null;
    }
    this.micConnected = false;
    this.meter = 0;
  }

  // --- PTT --------------------------------------------------------------

  async start(): Promise<void> {
    if (this.recording) return;
    try {
      await this.startMicCapture();
    } catch {
      this.setStage('error', 'mic blocked');
      return;
    }
    if (this.audioCtx && this.audioCtx.state === 'suspended') {
      try { await this.audioCtx.resume(); } catch { /* ignore */ }
    }
    this.resetPcm();
    this.recording = true;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ type: 'start' })); } catch { /* ignore */ }
    } else {
      this.setStage('recording', 'recording (offline)…');
    }
  }

  async end(): Promise<void> {
    if (!this.recording) return;
    this.recording = false;
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ type: 'end' })); } catch { /* ignore */ }
      return;
    }
    // HTTP fallback.
    const pcm = this.takePcm();
    if (!pcm) {
      this.setStage('idle', 'idle');
      return;
    }
    this.setStage('transcribing', 'transcribing…');
    try {
      const text = await this.postPcm(pcm);
      if (text) this.fireResult(text);
      this.setStage('idle', 'idle');
    } catch (err) {
      this.setStage('error', 'stt failed');
      this.lastError = (err as Error).message;
    }
  }

  cancel(): void {
    if (!this.recording) return;
    this.recording = false;
    this.resetPcm();
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify({ type: 'cancel' })); } catch { /* ignore */ }
    }
    this.setStage('idle', 'idle');
  }

  // --- helpers ----------------------------------------------------------

  private setStage(stage: VoiceStage, text: string): void {
    this.stage = stage;
    this.stageText = text || stage;
  }

  private resetPcm(): void {
    this.pcmBuf = [];
    this.pcmBytes = 0;
  }

  private takePcm(): Uint8Array | null {
    if (!this.pcmBytes) { this.resetPcm(); return null; }
    const out = new Uint8Array(this.pcmBytes);
    let off = 0;
    for (const chunk of this.pcmBuf) {
      out.set(chunk, off);
      off += chunk.byteLength;
    }
    this.resetPcm();
    return out;
  }

  private async postPcm(pcm: Uint8Array): Promise<string> {
    const r = await fetch(STT_HTTP_PATH, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: pcm,
    });
    let json: { text?: string; detail?: unknown } | null = null;
    try { json = await r.json(); } catch { /* ignore */ }
    if (!r.ok) {
      const detail = json && json.detail ? JSON.stringify(json.detail) : '';
      throw new Error(`POST /voice/stt → ${r.status}${detail ? `: ${detail.slice(0, 200)}` : ''}`);
    }
    return (json && json.text) || '';
  }
}

export const voice = new VoiceState();
