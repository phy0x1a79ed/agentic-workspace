<script lang="ts">
  import PttButton from './PttButton.svelte';
  import PttComposerShell from './PttComposerShell.svelte';
  import { apiFetch, AuthError, toWsUrl } from '@awm/client';
  // Vite-bundled worklet URL: `?url` returns the asset URL at build time.
  // The bundled file is fetched relative to the page origin, so it works
  // whether the page is served from /ui/ptt/, /ui/agent/, or anywhere
  // else under the hub.
  import workletUrl from './lib/audio/worklet.js?url';

  // The mic/STT transport: owns audio capture + the /svc/ptt session WS,
  // delegates the tab UI to PttComposerShell. The shell routes each
  // beginLiveChunk / updateLiveChunk / finalizeLiveChunk to the active tab.
  //
  // Voice gestures (both available at once, no mode switch):
  //   - HOLD the PttButton (or SPACE) → one PTT utterance ({type:start} →
  //     speak → {type:end}).
  //   - START CONVO → continuous session ({type:start, mode:continuous});
  //     the backend silence-segments each utterance. PAUSE gates mic audio
  //     for a manual thinking break without ending the statement.
  // Both feed the shell's single uneditable Voice chip list.

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
    // Offline-only: seed the Voice-tab chip list on mount.
    mockInitialChips?: string[];
    // Offline-only: scripted partial→finalize walk for vitest / dev pages.
    mockPartialScript?: PartialStep[];
  }
  let {
    onsend,
    onText,
    svcPrefix = '/svc/ptt',
    mockInitialChips,
    mockPartialScript,
  }: Props = $props();

  const isMock = $derived(!!(mockInitialChips || mockPartialScript));

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
  let paused = $state(false);

  let micLevel = $state(0);
  let pendingLevel = 0;
  let levelFrame = 0;

  function pumpLevel() {
    levelFrame = 0;
    micLevel = pendingLevel;
  }
  function postLevel(v: number) {
    pendingLevel = v;
    if (!levelFrame) levelFrame = requestAnimationFrame(pumpLevel);
  }
  function resetLevel() {
    if (levelFrame) { cancelAnimationFrame(levelFrame); levelFrame = 0; }
    pendingLevel = 0;
    micLevel = 0;
  }

  // --- Session open: POST /svc/ptt/session/stream → ws_path. ----------

  // Set false when a session-open failure is NOT worth retrying (auth —
  // needs a login, not a backoff). Service-down / network errors leave it
  // true so openSocket reschedules a reconnect.
  let sessionRetryable = true;

  async function openSession(): Promise<string | null> {
    sessionRetryable = true;
    try {
      const body = await apiFetch<{ ws_path: string }>(
        `${svcPrefix}/session/stream`,
        { method: 'POST', body: {} },
      );
      return body.ws_path;
    } catch (err) {
      if (err instanceof AuthError) {
        status = 'error';
        statusText = 'not logged in';
        const port = Number(location.port) || (location.protocol === 'https:' ? 443 : 80);
        loginUrl = `http://${location.hostname}:${port + 1}/`;
        sessionRetryable = false;
        return null;
      }
      status = 'error';
      statusText = `session open failed: ${(err as Error).message}`;
      return null;
    }
  }

  async function openSocket(): Promise<void> {
    const wsPath = await openSession();
    if (!wsPath) {
      // Service was down / unreachable at open time. The reconnect path
      // (onWsClose) never fires because no socket was created, so without
      // this the composer would wedge until a manual reload.
      if (sessionRetryable) scheduleReconnect();
      return;
    }
    try {
      ws = new WebSocket(toWsUrl(wsPath));
    } catch (err) {
      status = 'error';
      statusText = `ws open failed: ${(err as Error).message}`;
      scheduleReconnect();
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
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  function scheduleReconnect() {
    if (isMock) return;
    if (reconnectTimer) return; // one pending reconnect at a time
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      reconnectDelay = Math.min(reconnectDelay * 2, 10_000);
      void openSocket();
    }, reconnectDelay);
  }
  function onWsClose(_ev: CloseEvent) {
    ws = null;
    resetLevel();
    scheduleReconnect();
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
      // Paused convo: hold the mic. Not sending tail audio also keeps the
      // backend from silence-cutting (its partial loop skips when the tail
      // is below threshold), so a thinking break never ends the statement.
      const isHot = (recording || convoListening) && !paused;
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

  // --- PTT: hold to talk (one utterance per press). -----------------------

  async function onDown() {
    if (isMock) return;
    if (composer?.getTab() !== 'voice') return;
    if (convoListening) return; // PTT is disabled during a convo session
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
    if (isMock) return;
    if (!recording) return;
    recording = false;
    resetLevel();
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'end' }));
    }
  }

  // --- Convo: continuous start/stop + manual pause. -----------------------

  async function onConvoToggle() {
    if (isMock) return;
    if (recording) return; // can't start a convo session mid-PTT-press
    if (convoListening) {
      convoListening = false;
      paused = false;
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
    paused = false;
    composer?.beginLiveChunk();
    ws.send(JSON.stringify({ type: 'start', mode: 'continuous' }));
    status = 'recording';
    statusText = 'listening…';
  }

  function togglePause() {
    if (!convoListening) return;
    paused = !paused;
    if (paused) {
      resetLevel();
      statusText = 'paused';
    } else {
      statusText = 'listening…';
    }
  }

  function onTabSwitchRequest(target: 'text' | 'voice'): boolean {
    if (recording) return false; // don't yank the page mid-PTT-press
    if (target === 'text' && convoListening) {
      convoListening = false;
      paused = false;
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
    if (isMock) {
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
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
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
    initialChips={mockInitialChips}
  >
    {#snippet voiceControls()}
      <div class="mic-row">
        <div class="mic-meter" aria-hidden="true">
          <div class="mic-bar" style:transform="scaleX({micLevel})"></div>
        </div>
        <div class="ptt-slot">
          <PttButton disabled={convoListening} onpttdown={onDown} onpttup={onUp} />
        </div>
      </div>
      <div class="convo-row">
        <button
          type="button"
          class="ctl convo"
          class:active={convoListening}
          onclick={onConvoToggle}
        >{convoListening ? 'STOP' : 'START CONVO'}</button>
        <button
          type="button"
          class="ctl pause"
          class:active={paused}
          disabled={!convoListening}
          onclick={togglePause}
        >{paused ? 'RESUME' : 'PAUSE'}</button>
      </div>
    {/snippet}
  </PttComposerShell>

  <div class="status mono" data-status={status}>
    <span class="dot" class:active={status === 'recording' && !paused}></span>
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

  .mic-row {
    display: flex;
    align-items: stretch;
    gap: var(--space-2, 8px);
  }
  .mic-meter {
    flex: 0 0 40px;
    align-self: stretch;
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    overflow: hidden;
    position: relative;
    min-height: 52px;
  }
  .mic-bar {
    position: absolute;
    inset: 0;
    background: linear-gradient(to top,
      color-mix(in oklab, var(--recording, #f55) 65%, transparent) 0%,
      color-mix(in oklab, var(--recording, #f55) 25%, transparent) 100%);
    transform-origin: left center;
    transform: scaleX(0);
    transition: transform 60ms linear;
  }
  .ptt-slot {
    flex: 1 1 auto;
    display: flex;
  }
  .ptt-slot :global(*) {
    flex: 1 1 auto;
  }

  .convo-row {
    display: flex;
    gap: var(--space-2, 8px);
  }
  .ctl {
    flex: 1 1 auto;
    min-height: 44px;
    padding: 0 14px;
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    color: var(--text2, #bbb);
    font-family: var(--mono, monospace);
    font-size: 12px;
    letter-spacing: 1px;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .ctl:hover:not(:disabled) { background: var(--surface3, #2a2a2a); color: var(--text, #ddd); }
  .ctl:disabled { opacity: 0.35; cursor: not-allowed; }
  .ctl.convo.active {
    background: color-mix(in oklab, var(--recording, #f55) 25%, var(--surface2, #222));
    border-color: var(--recording, #f55);
    color: var(--text, #ddd);
  }
  .ctl.pause.active {
    border-color: var(--atomizer, #ffb74d);
    color: var(--text, #ddd);
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
