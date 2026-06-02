<script lang="ts">
  import { base } from '$app/paths';
  import PttButton from './PttButton.svelte';
  import TranscriptHistory from './TranscriptHistory.svelte';

  // The wired panel composes the dumb PttButton + TranscriptHistory and owns
  // EVERYTHING stateful: WS lifecycle, mic capture, frame protocol, history.
  // Mock mode: pass `mockEntries` to skip all browser-API setup and seed
  // entries for offline component-dev iteration.

  type PartialStep = { delayMs: number; text: string | null };

  interface Props {
    mockEntries?: string[];
    // Offline-only: drive `currentPartial` through a scripted walk so the
    // streaming UI can be exercised under vitest / on /dev/components.
    // Ignored unless `mockEntries` is set (mock branch only).
    mockPartialScript?: PartialStep[];
  }
  let { mockEntries, mockPartialScript }: Props = $props();

  let entries = $state<string[]>([]);
  let currentPartial = $state<string | null>(null);
  let status = $state<'idle' | 'recording' | 'transcribing' | 'error'>('idle');
  let statusText = $state('');
  let loginUrl = $state<string | null>(null);

  let ws: WebSocket | null = null;
  let audioCtx: AudioContext | null = null;
  let micStream: MediaStream | null = null;
  let workletNode: AudioWorkletNode | null = null;
  let sourceNode: MediaStreamAudioSourceNode | null = null;
  let recording = false;

  function openSocket() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}${base}/_api/stream`;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      status = 'error';
      statusText = `ws open failed: ${(err as Error).message}`;
      return;
    }
    ws.binaryType = 'arraybuffer';
    ws.onmessage = onWsMessage;
    ws.onclose = onWsClose;
    ws.onerror = () => {
      status = 'error';
      statusText = 'ws error';
    };
  }

  let reconnectDelay = 1000;
  function onWsClose(ev: CloseEvent) {
    ws = null;
    if (mockEntries) return;
    if (ev.code === 1008) {
      // Unauthorized: no awm_session cookie. Browser won't gain one by
      // reconnecting — surface a login link instead of looping.
      status = 'error';
      statusText = 'not logged in';
      const port = Number(location.port) || (location.protocol === 'https:' ? 443 : 80);
      loginUrl = `http://${location.hostname}:${port + 1}/`;
      return;
    }
    setTimeout(() => {
      reconnectDelay = Math.min(reconnectDelay * 2, 10_000);
      openSocket();
    }, reconnectDelay);
  }

  function onWsMessage(ev: MessageEvent) {
    if (typeof ev.data !== 'string') return;
    let msg: any;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'stt_result' && typeof msg.text === 'string' && msg.text) {
      entries = [...entries, msg.text];
      currentPartial = null;
      status = 'idle';
      statusText = '';
      reconnectDelay = 1000;
    } else if (msg.type === 'partial' && typeof msg.text === 'string') {
      currentPartial = msg.text;
    } else if (msg.type === 'status') {
      status = msg.stage as typeof status;
      statusText = msg.text ?? '';
    } else if (msg.type === 'error') {
      status = 'error';
      statusText = msg.message ?? 'error';
    }
  }

  async function ensureMic() {
    if (workletNode) return;
    audioCtx = new AudioContext({ sampleRate: 16000 });
    await audioCtx.audioWorklet.addModule(`${base}/mic-worklet.js`);
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    sourceNode = audioCtx.createMediaStreamSource(micStream);
    workletNode = new AudioWorkletNode(audioCtx, 'mic-downsampler');
    workletNode.port.onmessage = (ev) => {
      if (!recording || !ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(ev.data as ArrayBuffer);
    };
    sourceNode.connect(workletNode);
  }

  async function onDown() {
    if (mockEntries) return;
    try {
      await ensureMic();
    } catch (err) {
      status = 'error';
      statusText = `mic error: ${(err as Error).message}`;
      return;
    }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      status = 'error';
      statusText = 'ws not connected';
      return;
    }
    recording = true;
    currentPartial = null;
    ws.send(JSON.stringify({ type: 'start' }));
  }

  function onUp() {
    if (mockEntries) return;
    if (!recording) return;
    recording = false;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'end' }));
    }
  }

  $effect(() => {
    if (mockEntries) {
      entries = [...mockEntries];
      const timers: ReturnType<typeof setTimeout>[] = [];
      if (mockPartialScript && mockPartialScript.length) {
        status = 'recording';
        let elapsed = 0;
        for (const step of mockPartialScript) {
          elapsed += step.delayMs;
          timers.push(setTimeout(() => {
            currentPartial = step.text;
            if (step.text === null) status = 'idle';
          }, elapsed));
        }
      }
      return () => {
        for (const t of timers) clearTimeout(t);
      };
    }
    openSocket();
    return () => {
      if (ws) try { ws.close(); } catch { /* ignore */ }
      if (sourceNode) try { sourceNode.disconnect(); } catch { /* ignore */ }
      if (workletNode) try { workletNode.disconnect(); } catch { /* ignore */ }
      if (micStream) micStream.getTracks().forEach((t) => t.stop());
      if (audioCtx) try { audioCtx.close(); } catch { /* ignore */ }
    };
  });
</script>

<section class="panel">
  <TranscriptHistory {entries} {currentPartial} live={status === 'recording'} />
  <div class="status mono" data-status={status}>
    <span class="dot" class:active={status === 'recording'}></span>
    <span class="txt">
      {status}{statusText ? ` · ${statusText}` : ''}
      {#if loginUrl}
        · <a href={loginUrl} target="_blank" rel="noopener">log in</a>
      {/if}
    </span>
  </div>
  <PttButton onpttdown={onDown} onpttup={onUp} />
</section>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    max-width: 480px;
  }
  .status {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--text3);
    min-height: 16px;
  }
  .status[data-status='error'] { color: var(--warn); }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text3);
    transition: background 0.12s;
  }
  .dot.active {
    background: var(--recording);
    box-shadow: 0 0 6px var(--recording);
  }
</style>
