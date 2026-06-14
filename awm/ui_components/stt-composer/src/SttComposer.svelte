<script lang="ts">
  import SttButton from './SttButton.svelte';
  import SttComposerShell from './SttComposerShell.svelte';
  import { apiFetch, AuthError, toWsUrl } from '@awm/client';
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
    /** Optional: also called on every silence-segmented final transcript,
     *  even when the user hasn't hit Send. Useful for "auto-post each
     *  finalized utterance" wiring (the agent page hooks this). */
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
  }
  let {
    onsend,
    onText,
    svcPrefix = '/svc/stt',
    chatContext,
    mockInitialChips,
    mockPartialScript,
  }: Props = $props();

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
    if (msg.type === 'stt_result' && typeof msg.text === 'string') {
      // PTT path: one finalized chip per press. (Convo mode no longer emits
      // stt_result — it sends composer/submit instead.)
      composer?.finalizeLiveChunk(msg.text);
      if (msg.text.trim()) onText?.(msg.text);
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
      // pipeline is visible. One guard: while locally paused, ignore a
      // 'recording' frame so it doesn't clobber the local 'paused' note.
      if (!(paused && msg.stage === 'recording')) {
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
      resetConvoText();
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
    resetConvoText();
    ws.send(JSON.stringify({ type: 'start', mode: 'continuous', context: contextSlice() }));
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

  // The single convo control folds START / PAUSE / RESUME into one
  // play/pause toggle: idle → start a continuous session; listening →
  // pause (thinking break, session stays open); paused → resume. There is
  // no STOP state on a play/pause toggle — a convo session is torn down by
  // switching to the TEXT tab (see onTabSwitchRequest).
  function onConvoButton() {
    if (!convoListening) {
      void onConvoToggle(); // start
    } else {
      togglePause();        // pause / resume
    }
  }

  function onTabSwitchRequest(target: 'text' | 'voice'): boolean {
    if (recording) return false; // don't yank the page mid-PTT-press
    if (target === 'text' && convoListening) {
      convoListening = false;
      paused = false;
      resetLevel();
      resetConvoText();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'end' }));
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
    ws.send(JSON.stringify({ type: 'context', text: (ctx ?? '').slice(-CONTEXT_CAP) }));
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
    initialChips={mockInitialChips}
  >
    {#snippet voiceControls()}
      <SttButton disabled={convoListening} onpttdown={onDown} onpttup={onUp} />
      <button
        type="button"
        class="ctl convo"
        class:active={convoListening}
        class:paused
        onclick={onConvoButton}
        aria-pressed={convoListening}
        aria-label={!convoListening ? 'start conversation' : paused ? 'resume conversation' : 'pause conversation'}
      >
        <span class="conv-icon" aria-hidden="true">{convoListening && !paused ? '⏸' : '▶'}</span>
        <span class="conv-lbl">{!convoListening ? 'CONVO' : paused ? 'RESUME' : 'PAUSE'}</span>
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
      class:active={status === 'recording' && !paused}
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
  .ctl.convo.active.paused {
    background: color-mix(in oklab, var(--atomizer, #ffb74d) 18%, var(--surface2, #222));
    border-color: var(--atomizer, #ffb74d);
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
