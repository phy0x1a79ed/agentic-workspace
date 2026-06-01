<script lang="ts">
  import TtsConfig from '$lib/components/TtsConfig.svelte';
  import TtsHistory from '$lib/components/TtsHistory.svelte';
  import { TtsCall } from '$lib/api/tts';

  interface Bubble { id: string; text: string; }
  let bubbles = $state<Bubble[]>([]);
  let nextId = 0;

  let call: TtsCall | null = null;
  let activeEngine: string | null = null;
  let activeParams: Record<string, unknown> = {};

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
    activeEngine = engine;
    activeParams = { ...params };
    return call;
  }

  async function handleSpeak(text: string, engine: string, params: Record<string, unknown>) {
    const id = `b${nextId++}`;
    bubbles = [...bubbles, { id, text }];
    const c = await ensureCall(engine, params);
    await c.play(text);
  }

  async function handleReplay(text: string) {
    if (!call) return;
    await call.play(text);
  }

  async function handleEngineChange(engine: string, params: Record<string, unknown>) {
    if (!call) return;
    if (activeEngine === engine && paramsEqual(activeParams, params)) return;
    await call.reconfigure(engine, params);
    activeEngine = engine;
    activeParams = { ...params };
  }
</script>

<main>
  <header class="top">
    <h1>awm tts</h1>
    <span class="sub">text → audio · live engine config</span>
  </header>

  <section class="history">
    <TtsHistory {bubbles} onspeak={handleReplay} />
  </section>

  <section class="composer">
    <TtsConfig onspeak={handleSpeak} onengine={handleEngineChange} />
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
