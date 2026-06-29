<script lang="ts">
  /**
   * TTS dev page — a TTS-engine config tester, now mounted on the standardized
   * <Chat> composite (from @awm/chat) so it shares the same transcript + text
   * input as the agent / stt pages. The tts scope owns + iterates the history
   * and TTS-playback sub-components; this page exercises them by injecting a
   * data source: `posts` (the spoken utterances) + `onSend` (synthesize the
   * typed text with the current engine/params and play it). The tts-specific UI
   * (engine config, presets) rides the Chat `header` slot; the live round-trip
   * controls (volume, status, cancel) ride the `aside` slot.
   */
  import { Chat, type Post } from '@awm/chat';
  import { Slider } from '@awm/primitives';
  import TtsConfig from '$lib/components/TtsConfig.svelte';
  import { TtsCall } from '$lib/api/tts';
  import { persistedState } from '$lib/persistedState.svelte';
  import type { CallStatus } from '$lib/status';
  import { onDestroy } from 'svelte';

  // Spoken utterances as transcript posts (the shared <TtsHistory> shape). The
  // `user:tts` author makes each row replayable via the history's speaker button.
  let posts = $state<Post[]>([]);
  let nextId = 0;

  // Engine selection state, shared with the config cards (a preset writes here).
  let selected = $state<string>('');
  let params = $state<Record<string, unknown>>({});

  let call: TtsCall | null = null;
  let activeEngine: string | null = null;
  let activeParams: Record<string, unknown> = {};

  // Playback gain, persisted so the slider survives reload. Read vol.value
  // unconditionally before the optional chain so Svelte registers the dep on
  // first run (when call is null and `call?.setVolume(v)` would skip the arg).
  const vol = persistedState<number>('volume', 1.0);
  $effect(() => {
    const v = vol.value;
    call?.setVolume(v);
  });

  // Round-trip status of the most recent speak/replay, rendered as a pill.
  let status = $state<CallStatus>({ kind: 'idle' });
  let tickHandle: ReturnType<typeof setInterval> | null = null;

  function startTimer() {
    if (tickHandle) clearInterval(tickHandle);
    tickHandle = setInterval(() => {
      if (status.kind === 'sending') {
        status = { ...status, tick: performance.now() };
      }
    }, 60);
  }
  function stopTimer() {
    if (tickHandle) { clearInterval(tickHandle); tickHandle = null; }
  }
  onDestroy(stopTimer);

  function paramsEqual(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
    const ka = Object.keys(a), kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every((k) => Object.is(a[k], b[k]));
  }

  async function ensureCall(engine: string, params: Record<string, unknown>): Promise<TtsCall> {
    if (call && activeEngine === engine && paramsEqual(activeParams, params)) {
      return call;
    }
    if (call && activeEngine && (activeEngine !== engine || !paramsEqual(activeParams, params))) {
      await call.reconfigure(engine, params);
      activeEngine = engine;
      activeParams = { ...params };
      return call;
    }
    call = await TtsCall.open(engine, params);
    call.setVolume(vol.value);
    activeEngine = engine;
    activeParams = { ...params };
    return call;
  }

  async function timed<T>(fn: () => Promise<T>): Promise<T> {
    const startedAt = performance.now();
    status = { kind: 'sending', startedAt, tick: startedAt };
    startTimer();
    try {
      const out = await fn();
      const latencyMs = performance.now() - startedAt;
      status = { kind: 'done', latencyMs };
      return out;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      status = { kind: 'error', message };
      throw err;
    } finally {
      stopTimer();
    }
  }

  // <Chat> calls this once per turn (the send-once gate lives in Chat). Echo the
  // text as a transcript row, then synthesize + play it on the current engine.
  async function onSend(text: string) {
    const t = text.trim();
    if (!t) return;
    if (!selected) {
      status = { kind: 'error', message: 'select an engine first' };
      return;
    }
    posts = [...posts, { id: `b${nextId++}`, author: 'user:tts', body: t, ts: new Date().toISOString() }];
    await timed(async () => {
      const c = await ensureCall(selected, params);
      c.setVolume(vol.value);
      await c.play(t);
    });
  }

  // A history row's replay button: speak that row's text on the current engine.
  async function onReplay(post: Post) {
    const t = (post.body ?? '').trim();
    if (!t || !selected) return;
    await timed(async () => {
      const c = await ensureCall(selected, params);
      c.setVolume(vol.value);
      await c.play(t);
    });
  }

  async function handleEngineChange(engine: string, params: Record<string, unknown>) {
    if (!call) return;
    if (activeEngine === engine && paramsEqual(activeParams, params)) return;
    await call.reconfigure(engine, params);
    activeEngine = engine;
    activeParams = { ...params };
  }

  function handleCancel() {
    if (call) {
      call.cancel();
      call = null;
      activeEngine = null;
      activeParams = {};
    }
    stopTimer();
    status = { kind: 'cancelled' };
  }

  async function handlePresetLoad(engine: string, nextParams: Record<string, unknown>) {
    selected = engine;
    params = { ...nextParams };
    if (call) {
      await call.reconfigure(engine, params);
      activeEngine = engine;
      activeParams = { ...params };
    }
  }

  function fmtMs(ms: number): string {
    if (ms < 1000) return `${Math.round(ms)} ms`;
    return `${(ms / 1000).toFixed(2)} s`;
  }
  const elapsedMs = $derived(
    status.kind === 'sending' ? Math.max(0, status.tick - status.startedAt) : 0,
  );
</script>

<main>
  <header class="top">
    <h1>awm tts</h1>
    <span class="sub">text → audio · live engine config</span>
  </header>

  <div class="chat-host">
    <Chat {posts} {onSend} {onReplay}>
      {#snippet header()}
        <TtsConfig
          bind:selected
          bind:params
          onengine={handleEngineChange}
          onpresetload={handlePresetLoad}
        />
      {/snippet}

      {#snippet aside()}
        <div class="tts-bar">
          <label class="vol" title="output volume">
            <span class="vol-icon" aria-hidden="true"
              >{vol.value === 0 ? 'x' : vol.value < 0.5 ? '▁' : vol.value < 0.9 ? '▄' : '█'}</span
            >
            <Slider
              value={vol.value}
              min={0}
              max={1}
              step={0.01}
              precision={2}
              onchange={(v) => (vol.value = v)}
            />
          </label>

          {#if status.kind === 'sending'}
            <button class="cancel mono" type="button" onclick={handleCancel}>cancel</button>
          {/if}

          <div class="status" aria-live="polite">
            {#if status.kind === 'sending'}
              <span class="dot dot-sending" aria-hidden="true"></span>
              <span class="status-text mono">sent · {fmtMs(elapsedMs)}</span>
            {:else if status.kind === 'done'}
              <span class="dot dot-done" aria-hidden="true"></span>
              <span class="status-text mono">audio in {fmtMs(status.latencyMs)}</span>
            {:else if status.kind === 'cancelled'}
              <span class="dot dot-cancelled" aria-hidden="true"></span>
              <span class="status-text mono">cancelled</span>
            {:else if status.kind === 'error'}
              <span class="dot dot-err" aria-hidden="true"></span>
              <span class="status-text status-err mono">{status.message}</span>
            {/if}
          </div>
        </div>
      {/snippet}
    </Chat>
  </div>
</main>

<style>
  main {
    display: flex;
    flex-direction: column;
    height: 100dvh;
    padding: var(--space-5);
    gap: var(--space-4);
    max-width: 900px;
    margin: 0 auto;
    box-sizing: border-box;
  }
  .top { display: flex; align-items: baseline; gap: var(--space-4); flex: 0 0 auto; }
  h1 {
    font-family: var(--mono);
    font-size: 15px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--atomizer);
    font-weight: 500;
  }
  .sub {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text3);
    letter-spacing: 1px;
  }
  .chat-host { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }

  .tts-bar {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-2) var(--space-4);
    flex-wrap: wrap;
  }
  .vol { display: inline-flex; align-items: center; gap: 6px; min-width: 120px; max-width: 180px; }
  .vol-icon { font-size: 14px; line-height: 1; user-select: none; }
  .cancel {
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    color: var(--text2, #bbb);
    font-size: 10px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    padding: 3px 10px;
    cursor: pointer;
  }
  .cancel:hover { color: var(--text, #ddd); border-color: var(--danger, #f55); }
  .status { display: inline-flex; align-items: center; gap: var(--space-2); margin-left: auto; min-height: 16px; }
  .status-text { font-size: 10px; letter-spacing: 0.4px; color: var(--text2); }
  .status-err { color: var(--danger); }
  .mono { font-family: var(--mono); }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot-sending {
    background: var(--atomizer);
    box-shadow: 0 0 0 0 color-mix(in oklab, var(--atomizer) 60%, transparent);
    animation: pulse 1100ms var(--ease-mech, ease) infinite;
  }
  .dot-done { background: var(--ok, #5fb872); opacity: 0.85; }
  .dot-cancelled { background: var(--text3); opacity: 0.85; }
  .dot-err { background: var(--danger); }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0   color-mix(in oklab, var(--atomizer) 55%, transparent); }
    70%  { box-shadow: 0 0 0 6px color-mix(in oklab, var(--atomizer)  0%, transparent); }
    100% { box-shadow: 0 0 0 0   color-mix(in oklab, var(--atomizer)  0%, transparent); }
  }
</style>
