/**
 * Thin STT client targeting the @awm/ptt stripe's backend at absolute
 * `/ptt/_api/stream`. Owns mic capture (via the bundled AudioWorklet) and
 * the WS frame protocol. Surface is a press-and-hold controller —
 * start() / end() / cancel() — with a partial→final event stream.
 *
 * Backend wire protocol (mirrors packages/ptt/backend/app.py):
 *   up:   {"type":"start"} then binary 16k mono int16 PCM frames then {"type":"end"};
 *         {"type":"cancel"} to abort
 *   down: {"type":"ready"} | {"type":"status",stage,...} |
 *         {"type":"partial",text} | {"type":"stt_result",text} | {"type":"error",message}
 */

const PTT_WS_PATH = '/ptt/_api/stream';
const WORKLET_PATH = './mic-worklet.js';

export interface SttClient {
  /** Grab mic + open WS lazily; idempotent. */
  ensureReady(): Promise<void>;
  /** Begin one press: sends {"type":"start"}; mic frames start flowing. */
  start(): Promise<void>;
  /** End the press: sends {"type":"end"}. Resolves with the final transcript
   * once the backend replies with stt_result (or rejects on error). */
  end(): Promise<string>;
  /** Abort an in-flight press without waiting for a transcript. */
  cancel(): void;
  /** Register a callback for streaming partial transcripts during a press. */
  onpartial(cb: (text: string) => void): () => void;
  /** Tear down mic + WS — call before the page unloads or the stripe unmounts. */
  dispose(): void;
}

class SttClientImpl implements SttClient {
  private ws: WebSocket | null = null;
  private wsReady: Promise<void> | null = null;
  private audioCtx: AudioContext | null = null;
  private micStream: MediaStream | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private recording = false;
  private partialCbs = new Set<(text: string) => void>();
  private pendingEnd: { resolve: (s: string) => void; reject: (e: Error) => void } | null = null;

  async ensureReady(): Promise<void> {
    if (!this.audioCtx) {
      this.audioCtx = new AudioContext({ sampleRate: 16000 });
      await this.audioCtx.audioWorklet.addModule(WORKLET_PATH);
      this.micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.sourceNode = this.audioCtx.createMediaStreamSource(this.micStream);
      this.workletNode = new AudioWorkletNode(this.audioCtx, 'mic-downsampler');
      this.workletNode.port.onmessage = (ev) => this.onFrame(ev.data as ArrayBuffer);
      this.sourceNode.connect(this.workletNode);
    }
    if (!this.ws || this.ws.readyState >= WebSocket.CLOSING) {
      await this.openWs();
    } else if (this.wsReady) {
      await this.wsReady;
    }
  }

  private openWs(): Promise<void> {
    const url = new URL(PTT_WS_PATH, window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(url.toString());
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    this.wsReady = new Promise<void>((resolve, reject) => {
      const onOpen = () => { cleanup(); resolve(); };
      const onErrFirst = () => { cleanup(); reject(new Error('ws error before open')); };
      const cleanup = () => {
        ws.removeEventListener('open', onOpen);
        ws.removeEventListener('error', onErrFirst);
      };
      ws.addEventListener('open', onOpen, { once: true });
      ws.addEventListener('error', onErrFirst, { once: true });
    });
    ws.addEventListener('message', (ev) => this.onWsMessage(ev));
    ws.addEventListener('close', () => {
      if (this.ws === ws) this.ws = null;
      if (this.pendingEnd) {
        this.pendingEnd.reject(new Error('ws closed before stt_result'));
        this.pendingEnd = null;
      }
    });
    return this.wsReady;
  }

  private onWsMessage(ev: MessageEvent): void {
    if (typeof ev.data !== 'string') return;
    let msg: { type?: string; text?: string; message?: string } = {};
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'partial' && typeof msg.text === 'string') {
      for (const cb of this.partialCbs) cb(msg.text);
    } else if (msg.type === 'stt_result' && typeof msg.text === 'string') {
      if (this.pendingEnd) {
        this.pendingEnd.resolve(msg.text);
        this.pendingEnd = null;
      }
    } else if (msg.type === 'error') {
      const err = new Error(msg.message ?? 'stt error');
      if (this.pendingEnd) { this.pendingEnd.reject(err); this.pendingEnd = null; }
    }
  }

  private onFrame(buf: ArrayBuffer): void {
    if (!this.recording) return;
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(buf);
  }

  async start(): Promise<void> {
    await this.ensureReady();
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error('ws not open');
    }
    this.recording = true;
    this.ws.send(JSON.stringify({ type: 'start' }));
  }

  end(): Promise<string> {
    return new Promise<string>((resolve, reject) => {
      if (!this.recording) {
        reject(new Error('not recording'));
        return;
      }
      this.recording = false;
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error('ws not open'));
        return;
      }
      this.pendingEnd = { resolve, reject };
      this.ws.send(JSON.stringify({ type: 'end' }));
    });
  }

  cancel(): void {
    if (this.recording) {
      this.recording = false;
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'cancel' }));
      }
    }
    if (this.pendingEnd) {
      this.pendingEnd.reject(new Error('cancelled'));
      this.pendingEnd = null;
    }
  }

  onpartial(cb: (text: string) => void): () => void {
    this.partialCbs.add(cb);
    return () => this.partialCbs.delete(cb);
  }

  dispose(): void {
    this.cancel();
    try { this.ws?.close(); } catch { /* ignore */ }
    this.ws = null;
    try { this.workletNode?.disconnect(); } catch { /* ignore */ }
    try { this.sourceNode?.disconnect(); } catch { /* ignore */ }
    try { this.micStream?.getTracks().forEach(t => t.stop()); } catch { /* ignore */ }
    try { this.audioCtx?.close(); } catch { /* ignore */ }
    this.workletNode = null;
    this.sourceNode = null;
    this.micStream = null;
    this.audioCtx = null;
  }
}

let _singleton: SttClient | null = null;
export function getStt(): SttClient {
  if (!_singleton) _singleton = new SttClientImpl();
  return _singleton;
}
