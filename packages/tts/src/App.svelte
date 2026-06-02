<script lang="ts">
  import TtsConfig from '$lib/components/TtsConfig.svelte';
  import TtsHistory from '$lib/components/TtsHistory.svelte';
  import TtsPresets from '$lib/components/TtsPresets.svelte';
  import { TtsCall, type EngineRegistry } from '$lib/api/tts';
  import { persistedState } from '$lib/persistedState.svelte';
  import type { CallStatus } from '$lib/status';
  import { onDestroy } from 'svelte';

  interface Bubble { id: string; text: string; }
  let bubbles = $state<Bubble[]>([]);
  let nextId = 0;

  // Composer state lifted here so TtsPresets and TtsConfig share the
  // same source of truth; presets writes into it on "load".
  let selected = $state<string>('');
  let params = $state<Record<string, unknown>>({});
  let engines = $state<EngineRegistry | null>(null);

  let call: TtsCall | null = null;
  let activeEngine: string | null = null;
  let activeParams: Record<string, unknown> = {};

  // Playback gain, 0 = mute, 1 = unity. Backed by the stripe-local
  // state service so the slider position survives a page reload.
  // The $effect re-runs on every vol.value change and pushes the new
  // gain into the live TtsCall's GainNode for live updates.
  const vol = persistedState<number>('volume', 1.0);
  $effect(() => { call?.setVolume(vol.value); });

  // Round-trip status of the most-recent speak/replay call. The composer
  // renders this as a sent/delay pill.
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

  async function handleSpeak(text: string, engine: string, params: Record<string, unknown>) {
    const id = `b${nextId++}`;
    bubbles = [...bubbles, { id, text }];
    await timed(async () => {
      const c = await ensureCall(engine, params);
      c.setVolume(vol.value);
      await c.play(text);
    });
  }

  async function handleReplay(text: string) {
    if (!call) return;
    await timed(async () => {
      call!.setVolume(vol.value);
      await call!.play(text);
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
</script>

<main>
  <header class="top">
    <h1>awm tts</h1>
    <span class="sub">text → audio · live engine config</span>
  </header>

  <section class="history">
    <TtsHistory {bubbles} {status} onspeak={handleReplay} oncancel={handleCancel} />
  </section>

  <section class="composer">
    <TtsConfig
      {status}
      bind:selected
      bind:params
      bind:engines
      volume={vol.value}
      onvolume={(v) => (vol.value = v)}
      onspeak={handleSpeak}
      onengine={handleEngineChange}
      oncancel={handleCancel}
    />
    <TtsPresets currentEngine={selected} currentParams={params} onload={handlePresetLoad} />
  </section>
</main>

<style>
  main {
    display: grid;
    grid-template-rows: auto 1fr auto;
    height: 100dvh;
    padding: var(--space-5);
    gap: var(--space-4);
    max-width: 900px;
    margin: 0 auto;
    box-sizing: border-box;
  }
  .top { display: flex; align-items: baseline; gap: var(--space-4); }
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
  .history { min-height: 0; display: flex; }
  .composer { flex-shrink: 0; }
</style>
