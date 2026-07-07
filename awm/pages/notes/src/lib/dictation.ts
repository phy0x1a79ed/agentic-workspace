// Dictation transport — drives the existing `stt` service's `stream` session so
// speech lands on the page as if typed. We deliberately do NOT reuse the
// stt-composer UI: this is a headless controller that emits two things to the
// editor —
//   • interim text  (the live, still-changing tail — shown ghosted at the caret)
//   • committed text (a finalized phrase — inserted at the caret for real)
//
// It runs the session in `continuous` mode: the backend streams `partial`
// frames (interim) and, after each speech→silence cut, a cumulative `composer`
// frame. Committing the *delta* of successive composer frames turns the stream
// into "phrase appears when you pause", which reads as natural dictation. The
// custom vocabulary is passed as whisper's `initial_prompt` so domain terms are
// spelled correctly.

import { svc, toWsUrl } from '@awm/client';
import workletUrl from './audio/worklet.js?url';

export type DictationState = 'idle' | 'connecting' | 'listening' | 'error';

export interface DictationHandlers {
  onInterim?: (text: string) => void;
  onCommit?: (text: string) => void;
  onState?: (state: DictationState, detail?: string) => void;
  onLevel?: (level: number) => void; // 0..1 mic RMS, for the pulse meter
}

/** Longest-common-prefix delta: the newly-added tail of `cur` vs `prev`. */
function commitDelta(prev: string, cur: string): string {
  if (cur.startsWith(prev)) return cur.slice(prev.length);
  let i = 0;
  while (i < prev.length && i < cur.length && prev[i] === cur[i]) i++;
  return cur.slice(i);
}

export function createDictation(h: DictationHandlers) {
  let ws: WebSocket | null = null;
  let audioCtx: AudioContext | null = null;
  let micStream: MediaStream | null = null;
  let workletNode: AudioWorkletNode | null = null;
  let sourceNode: MediaStreamAudioSourceNode | null = null;
  let recording = false;
  let prevComposer = '';
  let hotwords = '';

  function setState(s: DictationState, detail?: string) {
    h.onState?.(s, detail);
  }

  async function ensureMic(): Promise<void> {
    if (workletNode) return;
    audioCtx = new AudioContext({ sampleRate: 16000 });
    await audioCtx.audioWorklet.addModule(workletUrl);
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    sourceNode = audioCtx.createMediaStreamSource(micStream);
    workletNode = new AudioWorkletNode(audioCtx, 'mic-downsampler');
    workletNode.port.onmessage = (ev) => {
      if (!recording) return;
      const buf = ev.data as ArrayBuffer;
      const v = new Int16Array(buf);
      if (v.length > 0) {
        let sumsq = 0;
        for (let i = 0; i < v.length; i++) sumsq += v[i] * v[i];
        h.onLevel?.(Math.min(1, Math.sqrt(sumsq / v.length) / 3000));
      }
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(buf);
    };
    sourceNode.connect(workletNode);
  }

  async function ensureSocket(): Promise<void> {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    const { ws_path } = await svc('stt').session<{ ws_path: string }>('stream', {});
    await new Promise<void>((resolve, reject) => {
      const sock = new WebSocket(toWsUrl(ws_path));
      sock.binaryType = 'arraybuffer';
      sock.onopen = () => { ws = sock; resolve(); };
      sock.onerror = () => reject(new Error('stt socket error'));
      sock.onclose = () => {
        if (ws === sock) ws = null;
        if (recording) { recording = false; setState('idle'); }
      };
      sock.onmessage = onMessage;
    });
  }

  function onMessage(ev: MessageEvent) {
    if (typeof ev.data !== 'string') return;
    let msg: any;
    try { msg = JSON.parse(ev.data); } catch { return; }
    switch (msg.type) {
      case 'partial':
        if (typeof msg.text === 'string') h.onInterim?.(msg.text);
        break;
      case 'composer':
        if (typeof msg.text === 'string') {
          const delta = commitDelta(prevComposer, msg.text);
          prevComposer = msg.text;
          if (delta.trim()) { h.onCommit?.(delta); h.onInterim?.(''); }
        }
        break;
      case 'submit':
        // Whole message finished; its text was already committed via composer
        // deltas. Reset so the next message's composer starts fresh.
        prevComposer = '';
        h.onInterim?.('');
        break;
      case 'status':
        if (msg.stage === 'idle') setState('idle');
        break;
      case 'error':
        setState('error', msg.message ?? 'dictation error');
        break;
    }
  }

  return {
    /** Set the custom-vocab glossary sent as whisper's initial_prompt. */
    setHotwords(terms: string[]) {
      hotwords = terms.filter(Boolean).join(', ');
    },

    async start(): Promise<void> {
      if (recording) return;
      setState('connecting');
      try {
        await ensureSocket();
        await ensureMic();
        if (audioCtx?.state === 'suspended') await audioCtx.resume();
      } catch (err) {
        setState('error', (err as Error).message);
        return;
      }
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        setState('error', 'not connected');
        return;
      }
      recording = true;
      prevComposer = '';
      ws.send(JSON.stringify({
        type: 'start',
        mode: 'continuous',
        initial_prompt: hotwords || undefined,
      }));
      setState('listening');
    },

    stop(): void {
      if (!recording) return;
      recording = false;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'end' }));
      }
      h.onInterim?.('');
      h.onLevel?.(0);
      setState('idle');
    },

    get active(): boolean { return recording; },

    destroy(): void {
      recording = false;
      try { workletNode?.disconnect(); } catch { /* ignore */ }
      try { sourceNode?.disconnect(); } catch { /* ignore */ }
      try { micStream?.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
      try { void audioCtx?.close(); } catch { /* ignore */ }
      try { ws?.close(); } catch { /* ignore */ }
      ws = null; workletNode = null; sourceNode = null; micStream = null; audioCtx = null;
    },
  };
}
