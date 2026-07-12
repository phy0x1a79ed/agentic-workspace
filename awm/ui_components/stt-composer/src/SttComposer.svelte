<script lang="ts">
  import SttButton from './SttButton.svelte';
  import SttComposerShell from './SttComposerShell.svelte';
  import { apiFetch, AuthError, toWsUrl } from '@awm/client';
  import type { TelemetryEvent } from '@awm/stt-telemetry';
  // Vite-bundled worklet URL: `?url` returns the asset URL at build time.
  // The bundled file is fetched relative to the page origin, so it works
  // whether the page is served from /ui/stt/, /ui/agent/, or anywhere
  // else under the hub.
  import workletUrl from './lib/audio/worklet.js?url';

  // The mic/STT transport: owns audio capture + the /svc/stt session WS,
  // delegates the tab UI to SttComposerShell. The shell routes each
  // beginLiveChunk / updateLiveChunk / finalizeLiveChunk to the active tab.
  //
  // Voice gestures (both available at once, no mode switch):
  //   - HOLD the SttButton (or SPACE) → one PTT utterance ({type:start} →
  //     speak → {type:end}).
  //   - START CONVO → continuous session ({type:start, mode:continuous});
  //     the backend silence-segments each utterance. PAUSE gates mic audio
  //     for a manual thinking break without ending the statement.
  // PTT feeds the shell's uneditable Voice chip list (one chip per press);
  // Convo feeds a single flowing text panel (the accumulating cleaned message)
  // and posts each completed message to the chat history.

  type PartialStep = { delayMs: number; text: string | null };

  interface Props {
    /** Called with the final assembled text when the user hits Send. */
    onsend?: (text: string) => void;
    /** Optional auto-post signal: fired per finalized utterance WITHOUT a Send
     *  press — but only in **convo** mode (continuous, silence-segmented). PTT
     *  no longer fires this; it buffers chips that post once on Send (`onsend`).
     *  So onsend/onText are mutually exclusive per mode and one turn posts once. */
    onText?: (text: string) => void;
    /** Override the service prefix. Defaults to /svc/stt. */
    svcPrefix?: string;
    /** Recent chat-history context (page-owned) for the convo cleanup LLM.
     *  Sent up — capped to ~2k chars — on convo start and whenever it
     *  changes while a convo session is live. */
    chatContext?: string;
    // Offline-only: seed the Voice-tab chip list on mount.
    mockInitialChips?: string[];
    // Offline-only: scripted partial→finalize walk for vitest / dev pages.
    mockPartialScript?: PartialStep[];
    /** Dev-only: when provided, every pipeline event (each audio chunk sent,
     *  each control frame, every inbound frame, and the backend `metric` timing
     *  frames) is reported here for a telemetry panel. Providing this also opts
     *  the session into the backend metric stream (the start frame carries
     *  `telemetry:true`). Omit it in production — zero overhead, no metrics. */
    onTelemetry?: (e: TelemetryEvent) => void;
  }
  let {
    onsend,
    onText,
    svcPrefix = '/svc/stt',
    chatContext,
    mockInitialChips,
    mockPartialScript,
    onTelemetry,
  }: Props = $props();

  // --- Dev telemetry helpers (no-op unless onTelemetry is wired). ----------
  function telem(dir: TelemetryEvent['dir'], event: string, detail?: string, dur?: number) {
    onTelemetry?.({ t: performance.now(), dir, event, detail, dur });
  }
  // Short, render-cheap detail string for an inbound frame (truncate transcript
  // text; summarize status/ready/error).
  function shortDetail(msg: any): string {
    if (typeof msg.text === 'string') {
      return msg.text.length > 40 ? msg.text.slice(0, 40) + '…' : msg.text;
    }
    if (msg.type === 'status') return `${msg.stage}${msg.text ? ' · ' + msg.text : ''}`;
    if (msg.type === 'ready') return msg.user ?? '';
    if (msg.type === 'error') return msg.message ?? '';
    return '';
  }
  // Flatten a backend `metric` frame's extra fields into a `k=v` string (drop the
  // envelope keys already shown as columns).
  function metricDetail(msg: any): string {
    const skip = new Set(['type', 'event', 't', 'dur_ms']);
    return Object.entries(msg)
      .filter(([k, v]) => !skip.has(k) && v !== null && v !== undefined)
      .map(([k, v]) => `${k}=${v}`)
      .join(' ');
  }

  const isMock = $derived(!!(mockInitialChips || mockPartialScript));

  // Convo composer assembly. `convoComposer` is the LLM-cleaned accumulated
  // message (updated after each silence-cut); `convoPartial` is the live raw
  // utterance in progress. `convoText` is their join — the flowing message the
  // Voice pane renders (as text, not chips) while a convo session is live, so
  // you get both cleaned accumulation and word-by-word feedback. PTT uses none.
  const CONTEXT_CAP = 2000;
  let convoComposer = $state('');
  let convoPartial = $state('');
  const convoText = $derived((convoComposer + ' ' + convoPartial).trim());
  function resetConvoText() { convoComposer = ''; convoPartial = ''; }
  function contextSlice(): string { return (chatContext ?? '').slice(-CONTEXT_CAP); }

  let status = $state<'idle' | 'recording' | 'transcribing' | 'refining' | 'error'>('idle');
  let statusText = $state('');
  let loginUrl = $state<string | null>(null);

  let composer: SttComposerShell | null = $state(null);
  let ws: WebSocket | null = null;
  let audioCtx: AudioContext | null = null;
  let micStream: MediaStream | null = null;
  let workletNode: AudioWorkletNode | null = null;
  let sourceNode: MediaStreamAudioSourceNode | null = null;
  let recording = false;
  let convoListening = $state(false);

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

  // --- Session open: POST /svc/stt/session/stream → ws_path. ----------

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
    // Dev telemetry: backend metric frames are timing-only — log and stop, never
    // let them fall through the frame switch. Every other frame is logged (with a
    // short detail) and then dispatched normally.
    if (msg.type === 'metric') {
      telem('backend', msg.event, metricDetail(msg), msg.dur_ms);
      return;
    }
    telem('in', msg.type, shortDetail(msg));
    if (msg.type === 'stt_result' && typeof msg.text === 'string') {
      // PTT path: one finalized chip per press. (Convo mode no longer emits
      // stt_result — it sends composer/submit instead.)
      //
      // Per-mode send contract: PTT only *buffers* the utterance as a chip; it
      // does NOT auto-post. The accumulated chips post once when the user hits
      // SEND (→ `onsend`). Only convo mode's `submit` (below) auto-posts each
      // utterance via `onText`. This keeps `onsend` and `onText` mutually
      // exclusive per mode, so a single turn never posts twice.
      composer?.finalizeLiveChunk(msg.text);
      if (!convoListening) {
        status = 'idle';
        statusText = '';
      }
      reconnectDelay = 1000;
    } else if (msg.type === 'composer' && typeof msg.text === 'string') {
      // Convo: LLM-cleaned accumulated message after a silence-cut. The live
      // raw tail is now folded into it, so clear the tail and show the cleaned
      // text. The Voice pane reads `convoText` reactively — no chips here.
      convoComposer = msg.text;
      convoPartial = '';
    } else if (msg.type === 'submit' && typeof msg.text === 'string') {
      // Convo: message judged complete → post it to history and clear the
      // composer text so the panel is ready for the next message.
      if (msg.text.trim()) onText?.(msg.text);
      resetConvoText();
      reconnectDelay = 1000;
    } else if (msg.type === 'partial' && typeof msg.text === 'string') {
      if (convoListening) {
        // Live raw tail of the current utterance; `convoText` joins it onto the
        // cleaned accumulation from earlier silence-cuts.
        convoPartial = msg.text;
      } else {
        composer?.updateLiveChunk(msg.text);
      }
    } else if (msg.type === 'status') {
      // Apply server stages in convo mode too (refining, listening, …) so the
      // pipeline is visible.
      status = msg.stage as typeof status;
      statusText = msg.text ?? '';
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
      // Telemetry: report each chunk's size; the panel coalesces the burst.
      onTelemetry?.({ t: performance.now(), dir: 'out', event: 'audio_chunk', bytes: buf.byteLength });
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
    const startMsg: Record<string, unknown> = { type: 'start' };
    if (onTelemetry) startMsg.telemetry = true;
    ws.send(JSON.stringify(startMsg));
    telem('out', 'start');
  }

  function onUp() {
    if (isMock) return;
    if (!recording) return;
    recording = false;
    resetLevel();
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'end' }));
      telem('out', 'end');
    }
  }

  // --- Convo: continuous start / stop. ------------------------------------

  // The convo control is a plain START / STOP toggle: idle → start a continuous
  // session; listening → stop and tear it down.
  async function onConvoToggle() {
    if (isMock) return;
    if (recording) return; // can't start a convo session mid-PTT-press
    if (convoListening) {
      convoListening = false;
      resetLevel();
      resetConvoText();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'end' }));
        telem('out', 'end');
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
    resetConvoText();
    const startMsg: Record<string, unknown> = {
      type: 'start', mode: 'continuous', context: contextSlice(),
    };
    if (onTelemetry) startMsg.telemetry = true;
    ws.send(JSON.stringify(startMsg));
    telem('out', 'convo_start');
    status = 'recording';
    statusText = 'listening…';
  }

  // CLEAR during a convo session: wipe the accumulated composer text and send a
  // cancel so the backend aborts any in-flight work (partial/silence loops, a
  // pending submit, and the running refine) — nothing stale lands after. Ends
  // the session (back to idle); press CONVO to start again.
  function onConvoClear() {
    if (isMock) return;
    resetConvoText();
    resetLevel();
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'cancel' }));
      telem('out', 'cancel');
    }
    convoListening = false;
    status = 'idle';
    statusText = '';
  }

  function onTabSwitchRequest(target: 'text' | 'voice'): boolean {
    if (recording) return false; // don't yank the page mid-PTT-press
    if (target === 'text' && convoListening) {
      convoListening = false;
      resetLevel();
      resetConvoText();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'end' }));
        telem('out', 'end');
      }
      status = 'idle';
      statusText = '';
    }
    return true;
  }

  // Push recent-chat-history context up while a convo session is live, so the
  // cleanup LLM stays current as the conversation grows. Tracks chatContext
  // and convoListening; the start frame carries the initial slice.
  $effect(() => {
    const ctx = chatContext; // track
    if (isMock || !convoListening) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const slice = (ctx ?? '').slice(-CONTEXT_CAP);
    ws.send(JSON.stringify({ type: 'context', text: slice }));
    telem('out', 'context', `${slice.length}c`);
  });

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

<section class="panel" data-awm-component="SttComposer">
  <SttComposerShell
    bind:this={composer}
    {onsend}
    {onTabSwitchRequest}
    convo={convoListening}
    {convoText}
    onConvoClear={onConvoClear}
    initialChips={mockInitialChips}
  >
    {#snippet voiceControls()}
      <SttButton disabled={convoListening} onpttdown={onDown} onpttup={onUp} />
      <button
        type="button"
        class="ctl convo"
        class:active={convoListening}
        onclick={() => void onConvoToggle()}
        aria-pressed={convoListening}
        aria-label={convoListening ? 'stop conversation' : 'start conversation'}
      >
        <span class="conv-icon" aria-hidden="true">{convoListening ? '▮' : '▶'}</span>
        <span class="conv-lbl">{convoListening ? 'STOP' : 'CONVO'}</span>
      </button>
    {/snippet}

    {#snippet voiceMeter()}
      <div class="mic-meter" aria-hidden="true">
        <div class="mic-bar" style:transform="scaleX({micLevel})"></div>
      </div>
    {/snippet}
  </SttComposerShell>

  <div class="status mono" data-status={status}>
    <span
      class="dot"
      class:active={status === 'recording'}
      class:refining={status === 'refining'}
    ></span>
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
    width: 100%;
    /* Intrinsic floor so a content-hugging container (gallery card) gives a
       sensible width and a horizontal scrollbar engages predictably below it;
       inert when the parent is wider (agent/stt pages). */
    min-width: 300px;
  }

  /* Thin horizontal mic-level strip, rendered across the bottom of the
     voice surface via the {voiceMeter} snippet. */
  .mic-meter {
    width: 100%;
    height: 10px;
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 5px;
    overflow: hidden;
    position: relative;
  }
  .mic-bar {
    position: absolute;
    inset: 0;
    background: linear-gradient(to right,
      color-mix(in oklab, var(--recording, #f55) 25%, transparent) 0%,
      color-mix(in oklab, var(--recording, #f55) 70%, transparent) 100%);
    transform-origin: left center;
    transform: scaleX(0);
    transition: transform 60ms linear;
  }

  /* The single convo play/pause toggle, stacked in the right button column. */
  .ctl {
    min-height: 52px;
    padding: 0 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    color: var(--text2, #bbb);
    font-family: var(--mono, monospace);
    font-size: 11px;
    letter-spacing: 1px;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .ctl:hover:not(:disabled) { background: var(--surface3, #2a2a2a); color: var(--text, #ddd); }
  .ctl:disabled { opacity: 0.35; cursor: not-allowed; }
  .conv-icon { font-size: 13px; line-height: 1; }
  .ctl.convo.active {
    background: color-mix(in oklab, var(--recording, #f55) 25%, var(--surface2, #222));
    border-color: var(--recording, #f55);
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
  /* The LLM refine step: amber, pulsing, to read as "working" not "capturing". */
  .dot.refining {
    background: var(--atomizer, #ffb74d);
    box-shadow: 0 0 6px var(--atomizer, #ffb74d);
    animation: dot-pulse 1s ease-in-out infinite;
  }
  @keyframes dot-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
  }
</style>
