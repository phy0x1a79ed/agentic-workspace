/**
 * TTS service client. Talks to the registered service at
 *   POST /svc/tts/fn/<name>       (RPC functions)
 *   POST /svc/tts/session/call    (open a direct-bridge call session)
 *   WS   /svc/tts/session/<id>    (PCM + JSON control frames)
 *
 * URLs are absolute under /svc/tts/ — any page that mounts this lives at
 * its own /ui/<x>/ prefix and cross-prefixes the service explicitly.
 */

const SVC = '/svc/tts';

async function callFn<T>(fn: string, args: unknown = {}): Promise<T> {
  const r = await fetch(`${SVC}/fn/${fn}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(args),
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => '');
    throw new Error(`POST ${SVC}/fn/${fn} → HTTP ${r.status}: ${detail}`);
  }
  if (r.status === 204) return undefined as unknown as T;
  return (await r.json()) as T;
}

export interface EngineDescriptor {
  schema: Record<string, unknown>;
  defaults: Record<string, unknown>;
}

export type EngineRegistry = Record<string, EngineDescriptor>;

export async function listEngines(): Promise<EngineRegistry> {
  return await callFn<EngineRegistry>('listEngines');
}

interface SessionOpenResponse {
  session_id: string;
  ws_path: string;
  direct: boolean;
}

async function openCallSession(
  engine: string,
  params: Record<string, unknown>,
): Promise<SessionOpenResponse> {
  const r = await fetch(`${SVC}/session/call`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ engine, params }),
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => '');
    throw new Error(`POST ${SVC}/session/call → HTTP ${r.status}: ${detail}`);
  }
  return (await r.json()) as SessionOpenResponse;
}

interface PendingSpeak {
  resolve: (buf: AudioBuffer) => void;
  reject: (err: Error) => void;
  pcm: Uint8Array | null;
}

/**
 * One TTS call. Construct via `TtsCall.open(engine, params)` — opens
 * a session, gets back a ws_path, opens the WS. `speak(text)` sends an
 * utterance and resolves with the decoded AudioBuffer. Bytes go
 * byte-relayed across the hub bridge (no JSON wrap or base64).
 */
export class TtsCall {
  private ws: WebSocket;
  private ctx: AudioContext;
  private gain: GainNode;
  private queue: PendingSpeak[] = [];
  private ready: Promise<void>;
  private closed = false;
  private activeSource: AudioBufferSourceNode | null = null;

  static async open(engine: string, params: Record<string, unknown>): Promise<TtsCall> {
    const session = await openCallSession(engine, params);
    const httpUrl = new URL(session.ws_path, window.location.href);
    httpUrl.protocol = httpUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    return new TtsCall(httpUrl.toString());
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

  setVolume(v: number): void {
    if (!Number.isFinite(v)) return;
    const g = Math.max(0, Math.min(2, v));
    this.gain.gain.cancelScheduledValues(0);
    this.gain.gain.value = g;
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

  close(): void {
    this.cancel();
  }
}

/**
 * Convenience: open a single call, play one utterance, close.
 * For chat-history per-message replays where the caller doesn't want to
 * manage the call lifecycle. Spawns a new TtsCall per click — heavier than
 * reusing one for many utterances, but matches the "click → hear → done"
 * mental model of a per-row speaker button.
 */
export async function playOnce(
  text: string,
  engine: string,
  params: Record<string, unknown>,
): Promise<void> {
  const call = await TtsCall.open(engine, params);
  try {
    await call.play(text);
  } finally {
    call.close();
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
