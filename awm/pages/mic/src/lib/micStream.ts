// Headless transport for the remote mic: browser capture → the `mic` service's
// `stream` session → a `pacat` feeding the PulseAudio virtmic null-sink, so the
// phone's microphone becomes this machine's microphone.
//
// Kept separate from App.svelte so the four-state connection machine is
// reviewable by reading it rather than by tracing an inline script. The states
// are exactly the four the badge and the ring are coloured by:
//
//   connected  (green)  — idle; not streaming, nothing open
//   connecting (amber)  — pressed, session/socket still coming up
//   streaming  (purple) — the service confirmed `pacat` is running
//   offline    (red)    — the socket died before it ever opened
//
// The purple state is driven by the service's `ready` frame, NOT by the socket
// opening. An open socket says nothing about whether anything is recording:
// during a live outage the old page displayed "48 kHz · live" while `pacat` was
// dying on every connect.

import { svc, toWsUrl } from '@awm/client';
import workletUrl from './audio/worklet.js?url';

export type MicState = 'connected' | 'connecting' | 'streaming' | 'offline';

export interface MicHandlers {
  onState?: (state: MicState) => void;
  /** Status line under the button. `err` paints it red. */
  onMessage?: (text: string, err?: boolean) => void;
  /** Per-chunk peak amplitude, 0..1, for the ring meter. */
  onPeak?: (peak: number) => void;
}

const PING_MS = 4000;
const BACKOFF_MIN = 500;
const BACKOFF_MAX = 5000;

export function createMicStream(h: MicHandlers) {
  let ws: WebSocket | null = null;
  let ctx: AudioContext | null = null;
  let stream: MediaStream | null = null;
  let node: AudioWorkletNode | null = null;
  let src: MediaStreamAudioSourceNode | null = null;

  let started = false;      // audio graph acquired
  let on = false;           // user wants to stream
  let ready = false;        // service confirmed pacat is running
  let connFailed = false;   // a socket died before ever opening
  let backoff = BACKOFF_MIN;
  let reconnTimer: ReturnType<typeof setTimeout> | null = null;
  let pingTimer: ReturnType<typeof setInterval> | null = null;
  let rttMs: number | null = null;

  function state(): MicState {
    if (!on) return 'connected';
    if (ready) return 'streaming';
    if (connFailed) return 'offline';
    return 'connecting';
  }
  function emitState() { h.onState?.(state()); }
  function msg(text: string, err = false) { h.onMessage?.(text, err); }

  function liveLine(): string {
    const khz = ctx ? Math.round(ctx.sampleRate / 1000) : 48;
    return rttMs === null ? `${khz} kHz · live` : `${khz} kHz · live · ${rttMs} ms`;
  }

  // ── audio graph (acquired once, kept alive) ──
  //
  // Acquired BEFORE the session is opened, because the session-open payload
  // carries the sample rate and the service hands it straight to `pacat`. A
  // guessed rate used to be self-correcting; now it bakes a wrong rate in for
  // the session's life and pitch-shifts everything the machine records.
  async function ensureAudio(): Promise<boolean> {
    if (started) return true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (e) {
      msg('mic blocked: ' + (e as Error).message, true);
      return false;
    }
    ctx = new AudioContext();
    await ctx.audioWorklet.addModule(workletUrl);
    src = ctx.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ctx, 'pcm-worklet');
    node.port.onmessage = (e) => {
      const buf = e.data as ArrayBuffer;
      if (on && ws && ws.readyState === WebSocket.OPEN) ws.send(buf);
      const v = new Int16Array(buf);
      let p = 0;
      for (let i = 0; i < v.length; i++) { const a = v[i] < 0 ? -v[i] : v[i]; if (a > p) p = a; }
      h.onPeak?.(p / 32768);
    };
    // Deliberately NOT connected to ctx.destination: routing the phone's own
    // microphone to its own speaker is a feedback path, and the worklet is
    // driven without it.
    src.connect(node);
    await ctx.resume();
    started = true;
    return true;
  }

  // ── session + socket ──
  //
  // Every attempt opens a NEW session. The ws_path names one session on the
  // gateway; reusing a dead one loops forever on "unknown session".
  async function connect(): Promise<void> {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
    if (reconnTimer) { clearTimeout(reconnTimer); reconnTimer = null; }

    let path: string;
    try {
      const res = await svc('mic').session<{ ws_path: string }>('stream', {
        sampleRate: ctx ? Math.round(ctx.sampleRate) : 48000,
        channels: 1,
        format: 's16le',
      });
      path = res.ws_path;
    } catch (e) {
      if (!on) return;
      msg('could not open a mic session: ' + (e as Error).message, true);
      connFailed = true;
      emitState();
      retry();
      return;
    }
    if (!on) return; // stopped while the session was being opened

    let opened = false; // did THIS attempt reach onopen?
    const sock = new WebSocket(toWsUrl(path));
    sock.binaryType = 'arraybuffer';
    ws = sock;
    sock.onopen = () => {
      opened = true; connFailed = false; backoff = BACKOFF_MIN;
      msg('waiting for the audio path…');
      emitState();
      startPing();
    };
    sock.onmessage = (ev) => onFrame(ev);
    sock.onerror = () => { try { sock.close(); } catch { /* onclose does the work */ } };
    sock.onclose = () => {
      if (ws === sock) ws = null;
      ready = false; stopPing();
      if (!on) { emitState(); return; }   // normal stop → green
      if (!opened) {
        connFailed = true;
        msg('could not reach the mic service', true);
      }
      emitState();
      retry();
    };
  }

  function retry() {
    if (!on) return;
    reconnTimer = setTimeout(() => { void connect(); }, backoff);
    backoff = Math.min(backoff * 2, BACKOFF_MAX);
  }

  function onFrame(ev: MessageEvent) {
    if (typeof ev.data !== 'string') return;
    let m: { type?: string; message?: string; t?: number };
    try { m = JSON.parse(ev.data); } catch { return; }
    switch (m.type) {
      case 'ready':
        ready = true;
        emitState();
        msg(liveLine());
        break;
      case 'pong':
        if (typeof m.t === 'number') {
          rttMs = Math.max(0, Math.round(Date.now() - m.t));
          if (ready) msg(liveLine());
        }
        break;
      case 'superseded':
        halt(m.message || 'another device took the microphone');
        break;
      case 'error':
        // A service-side failure repeats on every reconnect, so stop rather
        // than spin: the operator needs to read this, not watch it flicker.
        halt(m.message || 'mic service error', true);
        break;
    }
  }

  /** Stop streaming without the user having asked — and without reconnecting. */
  function halt(text: string, err = false) {
    on = false; ready = false;
    if (reconnTimer) { clearTimeout(reconnTimer); reconnTimer = null; }
    stopPing();
    if (ws) { try { ws.close(); } catch { /* already gone */ } ws = null; }
    msg(text, err);
    emitState();
  }

  function startPing() {
    stopPing();
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: 'ping', t: Date.now() })); } catch { /* closing */ }
      }
    }, PING_MS);
  }
  function stopPing() {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  }

  return {
    get active(): boolean { return on; },
    get state(): MicState { return state(); },

    async start(): Promise<void> {
      if (on) return;
      if (!(await ensureAudio())) return;
      on = true; ready = false; connFailed = false; rttMs = null;
      backoff = BACKOFF_MIN;
      emitState();          // pressed → amber until the service says ready
      msg('connecting…');
      await connect();
    },

    stop(): void {
      if (!on) return;
      on = false; ready = false; connFailed = false;
      if (reconnTimer) { clearTimeout(reconnTimer); reconnTimer = null; }
      stopPing();
      if (ws) { try { ws.close(); } catch { /* already gone */ } ws = null; }
      msg('idle');
      emitState();
    },

    destroy(): void {
      on = false;
      if (reconnTimer) { clearTimeout(reconnTimer); reconnTimer = null; }
      stopPing();
      try { ws?.close(); } catch { /* ignore */ }
      try { node?.disconnect(); } catch { /* ignore */ }
      try { src?.disconnect(); } catch { /* ignore */ }
      try { stream?.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
      try { void ctx?.close(); } catch { /* ignore */ }
      ws = null; node = null; src = null; stream = null; ctx = null; started = false;
    },
  };
}
