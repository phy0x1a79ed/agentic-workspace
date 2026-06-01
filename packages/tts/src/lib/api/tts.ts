/**
 * TTS stripe client. Talks to the supervised python backend via
 * `<prefix>/_api/*` on the hub. Relative URLs keep the bundle
 * location-agnostic (works at /tts, /tts-rework, /dev-stuff/tts, ...).
 */

export interface EngineDescriptor {
  schema: Record<string, unknown>;
  defaults: Record<string, unknown>;
}

export type EngineRegistry = Record<string, EngineDescriptor>;

export async function listEngines(): Promise<EngineRegistry> {
  const r = await fetch('./_api/engines', { credentials: 'include' });
  if (!r.ok) throw new Error(`GET /engines → HTTP ${r.status}`);
  return (await r.json()) as EngineRegistry;
}

interface StartResponse {
  call_id: string;
  ws_url: string;
  expires_at: number;
  engine: string;
  instance_id: string;
}

async function startCall(engine: string, params: Record<string, unknown>): Promise<StartResponse> {
  const r = await fetch('./_api/call/start', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ engine, params }),
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => '');
    throw new Error(`POST /call/start → HTTP ${r.status}: ${detail}`);
  }
  return (await r.json()) as StartResponse;
}

interface PendingSpeak {
  resolve: (buf: AudioBuffer) => void;
  reject: (err: Error) => void;
  pcm: Uint8Array | null;
}

/**
 * One TTS call. Construct via `TtsCall.open(engine, params)` — opens
 * the WS lazily and returns a ready-to-use instance. `speak(text)`
 * sends a single utterance and resolves with the decoded AudioBuffer.
 * `reconfigure(engine, params)` swaps engines without re-opening.
 */
export class TtsCall {
  private ws: WebSocket;
  private ctx: AudioContext;
  private queue: PendingSpeak[] = [];
  private ready: Promise<void>;
  private closed = false;

  static async open(engine: string, params: Record<string, unknown>): Promise<TtsCall> {
    const start = await startCall(engine, params);
    // ws_url comes back relative (./_api/call/<id>); resolve against
    // the page URL and swap https→wss / http→ws.
    const httpUrl = new URL(start.ws_url, window.location.href);
    httpUrl.protocol = httpUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    return new TtsCall(httpUrl.toString());
  }

  private constructor(wsUrl: string) {
    this.ws = new WebSocket(wsUrl);
    this.ws.binaryType = 'arraybuffer';
    this.ctx = new AudioContext();
    this.ready = new Promise((resolve, reject) => {
      this.ws.addEventListener('open', () => resolve(), { once: true });
      this.ws.addEventListener('error', () => reject(new Error('ws error')), { once: true });
    });

    this.ws.addEventListener('message', (ev) => this.onMessage(ev));
    this.ws.addEventListener('close', () => {
      this.closed = true;
      for (const p of this.queue) p.reject(new Error('ws closed before done'));
      this.queue = [];
    });
  }

  private onMessage(ev: MessageEvent): void {
    const head = this.queue[0];
    if (!head) return;
    if (typeof ev.data === 'string') {
      let payload: { type?: string; sample_rate?: number; message?: string } = {};
      try { payload = JSON.parse(ev.data); } catch { return; }
      if (payload.type === 'done' && head.pcm) {
        this.queue.shift();
        const buf = pcm16ToAudioBuffer(this.ctx, head.pcm, payload.sample_rate ?? 24_000);
        head.resolve(buf);
      } else if (payload.type === 'error') {
        this.queue.shift();
        head.reject(new Error(payload.message ?? 'tts error'));
      }
    } else {
      // Binary PCM frame.
      const bytes = new Uint8Array(ev.data as ArrayBuffer);
      head.pcm = head.pcm ? concatBytes(head.pcm, bytes) : bytes;
    }
  }

  async speak(text: string): Promise<AudioBuffer> {
    if (this.closed) throw new Error('call closed');
    await this.ready;
    return new Promise<AudioBuffer>((resolve, reject) => {
      this.queue.push({ resolve, reject, pcm: null });
      this.ws.send(JSON.stringify({ type: 'speak', text }));
    });
  }

  async reconfigure(engine: string, params: Record<string, unknown>): Promise<void> {
    await this.ready;
    this.ws.send(JSON.stringify({ type: 'reconfigure', engine, params }));
  }

  async play(text: string): Promise<void> {
    const buf = await this.speak(text);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    src.start();
    await new Promise<void>((resolve) => {
      src.addEventListener('ended', () => resolve(), { once: true });
    });
  }

  close(): void {
    this.closed = true;
    try { this.ws.close(); } catch { /* ignore */ }
    try { this.ctx.close(); } catch { /* ignore */ }
  }
}

function concatBytes(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

function pcm16ToAudioBuffer(ctx: AudioContext, pcm: Uint8Array, sampleRate: number): AudioBuffer {
  // Engines emit raw little-endian int16 PCM (mono).
  const samples = pcm.length >> 1;
  const buf = ctx.createBuffer(1, samples, sampleRate);
  const channel = buf.getChannelData(0);
  const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  for (let i = 0; i < samples; i++) {
    channel[i] = view.getInt16(i * 2, true) / 32768;
  }
  return buf;
}
