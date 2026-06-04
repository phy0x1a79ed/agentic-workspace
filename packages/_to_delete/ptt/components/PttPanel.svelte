<script lang="ts">
  import { base } from '$app/paths';
  import PttButton from './PttButton.svelte';
  import PttComposerShell from './PttComposerShell.svelte';

  // The wired panel owns the WS lifecycle, mic capture, and frame protocol,
  // and delegates editable text + chunk UI to PttComposerShell. The shell
  // routes each beginLiveChunk / updateLiveChunk / finalizeLiveChunk call
  // to whichever tab is currently active (PTT or Convo). In Convo mode the
  // PttButton acts as a toggle and the backend auto-segments on silence.

  type PartialStep = { delayMs: number; text: string | null };

  interface Props {
    onsend?: (text: string) => void;
    // Offline-only: seed the PTT-tab editor on mount.
    mockInitial?: Array<string | { chunk?: string; text?: string }>;
    // Offline-only: seed the Convo-tab list on mount.
    mockInitialChips?: string[];
    // Offline-only: scripted partial→finalize walk for vitest /
    // /dev/components. A null `text` step fires finalize. Targets the
    // currently-active tab through the shell's dispatch.
    mockPartialScript?: PartialStep[];
  }
  let { onsend, mockInitial, mockInitialChips, mockPartialScript }: Props = $props();

  let status = $state<'idle' | 'recording' | 'transcribing' | 'error'>('idle');
  let statusText = $state('');
  let loginUrl = $state<string | null>(null);

  let composer: PttComposerShell | null = $state(null);
  let ws: WebSocket | null = null;
  let audioCtx: AudioContext | null = null;
  let micStream: MediaStream | null = null;
  let workletNode: AudioWorkletNode | null = null;
  let sourceNode: MediaStreamAudioSourceNode | null = null;
  let recording = false;          // PTT press is in flight (PTT-mode hold)
  let convoListening = $state(false); // Convo-mode continuous toggle is on

  // RMS meter coalescing — we compute per audio block (~10 ms) but only push
  // to the meter once per animation frame.
  let pendingLevel = 0;
  let levelFrame = 0;

  function pumpLevel() {
    levelFrame = 0;
    composer?.setMicLevel(pendingLevel);
  }
  function postLevel(v: number) {
    pendingLevel = v;
    if (!levelFrame) levelFrame = requestAnimationFrame(pumpLevel);
  }
  function resetLevel() {
    if (levelFrame) { cancelAnimationFrame(levelFrame); levelFrame = 0; }
    pendingLevel = 0;
    composer?.setMicLevel(0);
  }

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
    resetLevel();
    if (mockInitial || mockInitialChips) return;
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
      if (!convoListening) {
        // PTT mode (or convo end) — return to idle.
        status = 'idle';
        statusText = '';
      }
      reconnectDelay = 1000;
    } else if (msg.type === 'partial' && typeof msg.text === 'string') {
      composer?.updateLiveChunk(msg.text);
    } else if (msg.type === 'status') {
      // In convo mode the backend may flip to "idle" between silence cuts;
      // we keep our local recording-state authoritative.
      if (!convoListening || msg.stage === 'recording' || msg.stage === 'error') {
        status = msg.stage as typeof status;
        statusText = msg.text ?? '';
      }
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
      const isHot = recording || convoListening;
      if (!isHot) return;
      // RMS meter, computed on every block regardless of WS state so the
      // user sees mic-volume feedback even if the WS is reconnecting.
      const buf = ev.data as ArrayBuffer;
      const v = new Int16Array(buf);
      if (v.length > 0) {
        let sumsq = 0;
        for (let i = 0; i < v.length; i++) sumsq += v[i] * v[i];
        const rms = Math.sqrt(sumsq / v.length);
        // Normalize against ~3000 (empirical for speech-level int16).
        postLevel(Math.min(1, rms / 3000));
      }
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(buf);
    };
    sourceNode.connect(workletNode);
  }

  // --- PTT button dispatch — behavior depends on active tab. ---

  async function onDown() {
    if (mockInitial || mockInitialChips) return;
    const mode = composer?.getMode() ?? 'ptt';
    if (mode === 'convo') {
      await onConvoToggle();
      return;
    }
    // PTT mode — press-and-hold.
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
    if (mockInitial || mockInitialChips) return;
    const mode = composer?.getMode() ?? 'ptt';
    if (mode === 'convo') return; // toggle handled on down; ignore release
    if (!recording) return;
    recording = false;
    resetLevel();
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'end' }));
    }
  }

  async function onConvoToggle() {
    if (convoListening) {
      // Stop the listening session.
      convoListening = false;
      resetLevel();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'end' }));
      }
      status = 'idle';
      statusText = '';
      return;
    }
    // Start a fresh listening session.
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
    convoListening = true;
    composer?.beginLiveChunk();
    ws.send(JSON.stringify({ type: 'start', mode: 'continuous' }));
    status = 'recording';
    statusText = 'listening…';
  }

  // Block PTT-tab → Convo-tab while a PTT press is mid-flight; tear down
  // the Convo listening session cleanly when switching the other direction.
  function onTabSwitchRequest(target: 'ptt' | 'convo'): boolean {
    if (recording) return false;
    if (target === 'ptt' && convoListening) {
      convoListening = false;
      resetLevel();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'end' }));
      }
      status = 'idle';
      statusText = '';
    }
    return true;
  }

  $effect(() => {
    if (mockInitial || mockInitialChips) {
      const timers: ReturnType<typeof setTimeout>[] = [];
      if (mockPartialScript && mockPartialScript.length) {
        let elapsed = 0;
        let lastText = '';
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
      if (levelFrame) cancelAnimationFrame(levelFrame);
    };
  });
</script>

<section class="panel">
  <PttComposerShell
    bind:this={composer}
    {onsend}
    {onTabSwitchRequest}
    initialChunks={mockInitial}
    initialChips={mockInitialChips}
  >
    {#snippet ptt()}
      <PttButton onpttdown={onDown} onpttup={onUp} />
    {/snippet}
  </PttComposerShell>

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
