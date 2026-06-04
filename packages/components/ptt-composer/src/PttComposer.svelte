<script lang="ts">
  import PttButton from './PttButton.svelte';
  import PttComposerShell from './PttComposerShell.svelte';
  // Vite-bundled worklet URL: `?url` returns the asset URL at build time.
  // The bundled file is fetched relative to the page origin, so it works
  // whether the page is served from /ui/ptt/, /ui/agent/, or anywhere
  // else under the hub.
  import workletUrl from './lib/audio/worklet.js?url';

  // The mic/STT composer: owns audio capture + the /svc/ptt session WS,
  // delegates editable text + chunk UI to PttComposerShell. The shell
  // routes each beginLiveChunk / updateLiveChunk / finalizeLiveChunk call
  // to whichever tab is currently active (PTT or Convo). In Convo mode
  // PttButton acts as a toggle and the backend auto-segments on silence.

  type PartialStep = { delayMs: number; text: string | null };

  interface Props {
    /** Called with the final assembled text when the user hits Send. */
    onsend?: (text: string) => void;
    /** Optional: also called on every silence-segmented final transcript,
     *  even when the user hasn't hit Send. Useful for "auto-post each
     *  finalized utterance" wiring (the agent page hooks this). */
    onText?: (text: string) => void;
    /** Override the service prefix. Defaults to /svc/ptt. */
    svcPrefix?: string;
    // Offline-only: seed the PTT-tab editor on mount.
    mockInitial?: Array<string | { chunk?: string; text?: string }>;
    // Offline-only: seed the Convo-tab list on mount.
    mockInitialChips?: string[];
    // Offline-only: scripted partial→finalize walk for vitest / dev pages.
    mockPartialScript?: PartialStep[];
  }
  let {
    onsend,
    onText,
    svcPrefix = '/svc/ptt',
    mockInitial,
    mockInitialChips,
    mockPartialScript,
  }: Props = $props();

  let status = $state<'idle' | 'recording' | 'transcribing' | 'error'>('idle');
  let statusText = $state('');
  let loginUrl = $state<string | null>(null);

  let composer: PttComposerShell | null = $state(null);
  let ws: WebSocket | null = null;
  let audioCtx: AudioContext | null = null;
  let micStream: MediaStream | null = null;
  let workletNode: AudioWorkletNode | null = null;
  let sourceNode: MediaStreamAudioSourceNode | null = null;
  let recording = false;
  let convoListening = $state(false);

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

  // --- Session open: POST /svc/ptt/session/stream → ws_path. ----------

  async function openSession(): Promise<string | null> {
    try {
      const r = await fetch(`${svcPrefix}/session/stream`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({}),
      });
      if (r.status === 401 || r.status === 403) {
        status = 'error';
        statusText = 'not logged in';
        const port = Number(location.port) || (location.protocol === 'https:' ? 443 : 80);
        loginUrl = `http://${location.hostname}:${port + 1}/`;
        return null;
      }
      if (!r.ok) {
        status = 'error';
        statusText = `session open failed: HTTP ${r.status}`;
        return null;
      }
      const body = await r.json() as { ws_path: string };
      return body.ws_path;
    } catch (err) {
      status = 'error';
      statusText = `session open failed: ${(err as Error).message}`;
      return null;
    }
  }

  async function openSocket(): Promise<void> {
    const wsPath = await openSession();
    if (!wsPath) return;
    const httpUrl = new URL(wsPath, window.location.href);
    httpUrl.protocol = httpUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    try {
      ws = new WebSocket(httpUrl.toString());
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
  function onWsClose(_ev: CloseEvent) {
    ws = null;
    resetLevel();
    if (mockInitial || mockInitialChips) return;
    setTimeout(() => {
      reconnectDelay = Math.min(reconnectDelay * 2, 10_000);
      void openSocket();
    }, reconnectDelay);
  }

  function onWsMessage(ev: MessageEvent) {
    if (typeof ev.data !== 'string') return;
    let msg: any;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'stt_result' && typeof msg.text === 'string') {
      composer?.finalizeLiveChunk(msg.text);
      // Live-text callback fires on every silence segment (even in
      // convo mode), independent of the user pressing Send.
      if (msg.text.trim()) onText?.(msg.text);
      if (!convoListening) {
        status = 'idle';
        statusText = '';
      }
      reconnectDelay = 1000;
    } else if (msg.type === 'partial' && typeof msg.text === 'string') {
      composer?.updateLiveChunk(msg.text);
    } else if (msg.type === 'status') {
      if (!convoListening || msg.stage === 'recording' || msg.stage === 'error') {
        status = msg.stage as typeof status;
        statusText = msg.text ?? '';
      }
    } else if (msg.type === 'error') {
      status = 'error';
      statusText = msg.message ?? 'error';
    }
  }

  // --- Mic worklet wiring --------------------------------------------------

  async function ensureMic() {
    if (workletNode) return;
    audioCtx = new AudioContext({ sampleRate: 16000 });
    await audioCtx.audioWorklet.addModule(workletUrl);
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    sourceNode = audioCtx.createMediaStreamSource(micStream);
    workletNode = new AudioWorkletNode(audioCtx, 'mic-downsampler');
    workletNode.port.onmessage = (ev) => {
      const isHot = recording || convoListening;
      if (!isHot) return;
      const buf = ev.data as ArrayBuffer;
      const v = new Int16Array(buf);
      if (v.length > 0) {
        let sumsq = 0;
        for (let i = 0; i < v.length; i++) sumsq += v[i] * v[i];
        const rms = Math.sqrt(sumsq / v.length);
        postLevel(Math.min(1, rms / 3000));
      }
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(buf);
    };
    sourceNode.connect(workletNode);
  }

  // --- PTT button dispatch — behavior depends on active tab. --------------

  async function onDown() {
    if (mockInitial || mockInitialChips) return;
    const mode = composer?.getMode() ?? 'ptt';
    if (mode === 'convo') {
      await onConvoToggle();
      return;
    }
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
    if (mode === 'convo') return;
    if (!recording) return;
    recording = false;
    resetLevel();
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'end' }));
    }
  }

  async function onConvoToggle() {
    if (convoListening) {
      convoListening = false;
      resetLevel();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'end' }));
      }
      status = 'idle';
      statusText = '';
      return;
    }
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
    void openSocket();
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
    gap: var(--space-3, 12px);
    max-width: 480px;
  }
  .status {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--text3, #888);
    min-height: 16px;
  }
  .status[data-status='error'] { color: var(--warn, #f55); }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text3, #888);
    transition: background 0.12s;
  }
  .dot.active {
    background: var(--recording, #f55);
    box-shadow: 0 0 6px var(--recording, #f55);
  }
</style>
