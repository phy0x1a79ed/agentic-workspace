<script lang="ts">
  import { base } from '$app/paths';
  import PttButton from './PttButton.svelte';
  import PttComposer from './PttComposer.svelte';

  // The wired panel owns the WS lifecycle, mic capture, and frame protocol,
  // and delegates editable text + chunk UI to PttComposer. Each PTT press
  // is rendered inline as a live pill that updates with each `partial` and
  // gets finalized on `stt_result`. Send does NOT stop recording — if the
  // user hits Send mid-press, the composer clears and the next partial
  // spawns a fresh live pill in the now-empty editor.

  type PartialStep = { delayMs: number; text: string | null };

  interface Props {
    onsend?: (text: string) => void;
    // Offline-only: seed the composer's editor on mount with chunks +
    // optional plain-text gaps. Same shape as PttComposer.initialChunks.
    mockInitial?: Array<string | { chunk?: string; text?: string }>;
    // Offline-only: drive begin → update → finalize through a scripted walk
    // so the live-pill UI can be exercised under vitest / on /dev/components.
    // A null `text` step fires `finalize` (using the previous step's text).
    // Only honored when `mockInitial` is set (mock branch only).
    mockPartialScript?: PartialStep[];
  }
  let { onsend, mockInitial, mockPartialScript }: Props = $props();

  let status = $state<'idle' | 'recording' | 'transcribing' | 'error'>('idle');
  let statusText = $state('');
  let loginUrl = $state<string | null>(null);

  let composer: PttComposer | null = $state(null);
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
    if (mockInitial) return;
    if (ev.code === 1008) {
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
    if (msg.type === 'stt_result' && typeof msg.text === 'string') {
      composer?.finalizeLiveChunk(msg.text);
      status = 'idle';
      statusText = '';
      reconnectDelay = 1000;
    } else if (msg.type === 'partial' && typeof msg.text === 'string') {
      composer?.updateLiveChunk(msg.text);
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
    if (mockInitial) return;
    composer?.captureCaret();
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
    composer?.beginLiveChunk();
    ws.send(JSON.stringify({ type: 'start' }));
  }

  function onUp() {
    if (mockInitial) return;
    if (!recording) return;
    recording = false;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'end' }));
    }
  }

  $effect(() => {
    if (mockInitial) {
      const timers: ReturnType<typeof setTimeout>[] = [];
      if (mockPartialScript && mockPartialScript.length) {
        let elapsed = 0;
        let lastText = '';
        // Kick off the live pill at t=0.
        timers.push(setTimeout(() => {
          status = 'recording';
          composer?.beginLiveChunk();
        }, 0));
        for (const step of mockPartialScript) {
          elapsed += step.delayMs;
          const stepText = step.text;
          if (stepText === null) {
            const finalText = lastText;
            timers.push(setTimeout(() => {
              composer?.finalizeLiveChunk(finalText);
              status = 'idle';
            }, elapsed));
          } else {
            lastText = stepText;
            timers.push(setTimeout(() => {
              composer?.updateLiveChunk(stepText);
            }, elapsed));
          }
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
  <PttComposer
    bind:this={composer}
    {onsend}
    initialChunks={mockInitial}
  >
    {#snippet ptt()}
      <PttButton onpttdown={onDown} onpttup={onUp} />
    {/snippet}
  </PttComposer>

  <div class="status mono" data-status={status}>
    <span class="dot" class:active={status === 'recording'}></span>
    <span class="txt">
      {status}{statusText ? ` · ${statusText}` : ''}
      {#if loginUrl}
        · <a href={loginUrl} target="_blank" rel="noopener">log in</a>
      {/if}
    </span>
  </div>
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
