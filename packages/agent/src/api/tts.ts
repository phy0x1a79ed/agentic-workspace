/**
 * Thin TTS client targeting the @awm/tts stripe at absolute `/tts/_api/*`.
 *
 * Mirrors the WS protocol of `packages/tts/src/lib/api/tts.ts`'s TtsCall
 * class — speak request, binary PCM streaming, "done" finalizer with the
 * sample rate — but addresses absolute URLs since the bundle ships at
 * `/agent/`, not at `/tts/`.
 *
 * V1 picks the first engine returned by `/tts/_api/engines` with its
 * declared defaults. A picker / preset UI is out of scope for this stripe.
 */

const TTS_HTTP_BASE = '/tts/_api';

interface EngineDescriptor {
  schema: Record<string, unknown>;
  defaults: Record<string, unknown>;
}
type EngineRegistry = Record<string, EngineDescriptor>;

interface StartResponse {
  call_id: string;
  ws_url: string;
  expires_at: number;
  engine: string;
  instance_id: string;
}

interface PendingSpeak {
  resolve: (buf: AudioBuffer) => void;
  reject: (err: Error) => void;
  pcm: Uint8Array | null;
}

class TtsCall {
  private ws: WebSocket;
  private ctx: AudioContext;
  private gain: GainNode;
  private queue: PendingSpeak[] = [];
  private ready: Promise<void>;
  private closed = false;
  private activeSource: AudioBufferSourceNode | null = null;

  static async open(engine: string, params: Record<string, unknown>): Promise<TtsCall> {
    const start = await this.startCall(engine, params);
    // ws_url comes back relative (./_api/call/<id>); resolve it against the
    // TTS prefix root, not our agent prefix.
    const ttsRoot = new URL('/tts/', window.location.href);
    const httpUrl = new URL(start.ws_url, ttsRoot);
    httpUrl.protocol = httpUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    return new TtsCall(httpUrl.toString());
  }

  private static async startCall(engine: string, params: Record<string, unknown>): Promise<StartResponse> {
    const r = await fetch(`${TTS_HTTP_BASE}/call/start`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ engine, params }),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => '');
      throw new Error(`POST /tts/_api/call/start → HTTP ${r.status}: ${detail.slice(0, 200)}`);
    }
    return (await r.json()) as StartResponse;
  }

  private constructor(wsUrl: string) {
    this.ws = new WebSocket(wsUrl);
    this.ws.binaryType = 'arraybuffer';
    this.ctx = new AudioContext();
    this.gain = this.ctx.createGain();
    this.gain.connect(this.ctx.destination);
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

  async play(text: string): Promise<void> {
    const buf = await this.speak(text);
    if (this.closed) throw new Error('cancelled');
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gain);
    this.activeSource = src;
    src.start();
    await new Promise<void>((resolve) => {
      src.addEventListener('ended', () => resolve(), { once: true });
    });
    if (this.activeSource === src) this.activeSource = null;
  }

  cancel(): void {
    if (this.closed) return;
    this.closed = true;
    if (this.activeSource) {
      try { this.activeSource.stop(); } catch { /* already stopped */ }
      this.activeSource = null;
    }
    for (const p of this.queue) p.reject(new Error('cancelled'));
    this.queue = [];
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
  const samples = pcm.length >> 1;
  const buf = ctx.createBuffer(1, samples, sampleRate);
  const channel = buf.getChannelData(0);
  const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  for (let i = 0; i < samples; i++) {
    channel[i] = view.getInt16(i * 2, true) / 32768;
  }
  return buf;
}

async function listEngines(): Promise<EngineRegistry> {
  const r = await fetch(`${TTS_HTTP_BASE}/engines`, { credentials: 'include' });
  if (!r.ok) throw new Error(`GET /tts/_api/engines → HTTP ${r.status}`);
  return (await r.json()) as EngineRegistry;
}

let _call: TtsCall | null = null;
let _opening: Promise<TtsCall> | null = null;

async function ensureCall(): Promise<TtsCall> {
  if (_call) return _call;
  if (!_opening) {
    _opening = (async () => {
      const engines = await listEngines();
      const names = Object.keys(engines);
      if (names.length === 0) throw new Error('no TTS engines available');
      const first = names[0];
      const call = await TtsCall.open(first, engines[first].defaults);
      _call = call;
      return call;
    })().finally(() => { _opening = null; });
  }
  return _opening;
}

/** Play a single utterance through the lazy-init shared TtsCall. */
export async function play(text: string): Promise<void> {
  const trimmed = text.trim();
  if (!trimmed) return;
  const call = await ensureCall();
  await call.play(trimmed);
}

/** Cancel the current call; the next play() opens a fresh one. */
export function cancel(): void {
  _call?.cancel();
  _call = null;
}
