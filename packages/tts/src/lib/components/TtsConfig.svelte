<script lang="ts">
  import Button from '$lib/primitives/Button.svelte';
  import Card from '$lib/primitives/Card.svelte';
  import CollapsibleSection from '$lib/primitives/CollapsibleSection.svelte';
  import Select from '$lib/primitives/Select.svelte';
  import DynamicConfigForm from '$lib/primitives/DynamicConfigForm.svelte';
  import PanelLabel from '$lib/primitives/PanelLabel.svelte';
  import { listEngines, type EngineRegistry } from '$lib/api/tts';

  interface Props {
    onspeak?: (text: string, engine: string, params: Record<string, unknown>) => Promise<void> | void;
    onengine?: (engine: string, params: Record<string, unknown>) => Promise<void> | void;
  }
  let { onspeak, onengine }: Props = $props();

  let text = $state('');
  let engines = $state<EngineRegistry | null>(null);
  let loadError = $state<string | null>(null);
  let selected = $state<string>('');
  let params = $state<Record<string, unknown>>({});
  let configOpen = $state(true);
  let busy = $state(false);

  $effect(() => {
    listEngines()
      .then((reg) => {
        engines = reg;
        const ids = Object.keys(reg);
        if (ids.length && !selected) {
          selected = ids[0];
          params = { ...(reg[selected]?.defaults ?? {}) };
        }
      })
      .catch((err) => {
        loadError = err instanceof Error ? err.message : String(err);
      });
  });

  const engineIds = $derived(engines ? Object.keys(engines) : []);
  const currentSchema = $derived(
    engines && selected ? engines[selected]?.schema ?? {} : {}
  );

  function pickEngine(id: string) {
    selected = id;
    params = { ...(engines?.[id]?.defaults ?? {}) };
    void onengine?.(id, params);
  }

  function paramsChanged(next: Record<string, unknown>) {
    params = next;
    if (selected) void onengine?.(selected, params);
  }

  async function speak() {
    const t = text.trim();
    if (!t || !selected || busy) return;
    busy = true;
    try {
      await onspeak?.(t, selected, params);
      text = '';
    } finally {
      busy = false;
    }
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void speak();
    }
  }
</script>

<Card rail="manager">
  <div class="panel">
    <header class="head">
      <PanelLabel tone="atomizer">tts composer</PanelLabel>
      {#if selected}
        <span class="engine-label">{selected}</span>
      {/if}
    </header>

    <textarea
      class="ta mono"
      rows="3"
      placeholder="type something to speak (ctrl/⌘+enter to send)"
      bind:value={text}
      onkeydown={onKey}
      disabled={busy || !selected}
    ></textarea>

    <div class="actions">
      <Button
        kind="primary"
        size="md"
        onclick={speak}
        disabled={busy || !text.trim() || !selected}
      >{busy ? 'speaking…' : 'speak'}</Button>
      <span class="hint">ctrl/⌘+enter</span>
    </div>
  </div>
</Card>

<div class="config">
  <CollapsibleSection label="engine config" rail="plain" bind:open={configOpen}>
    {#if loadError}
      <div class="err">failed to load engines: {loadError}</div>
    {:else if !engines}
      <div class="loading">loading engines…</div>
    {:else}
      <div class="engine-row">
        <span class="lbl"><PanelLabel>engine</PanelLabel></span>
        <Select
          value={selected}
          options={engineIds}
          onchange={pickEngine}
          placeholder="pick an engine"
        />
      </div>
      <div class="schema">
        <DynamicConfigForm
          schema={currentSchema}
          value={params}
          onchange={paramsChanged}
        />
      </div>
    {/if}
  </CollapsibleSection>
</div>

<style>
  .panel { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-2) 0; }
  .head { display: flex; align-items: center; justify-content: space-between; }
  .engine-label {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--atomizer);
    letter-spacing: 1px;
    text-transform: uppercase;
  }
  .ta {
    width: 100%;
    padding: var(--space-3) var(--space-4);
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    font-size: 13px;
    line-height: 1.4;
    resize: vertical;
    min-height: 70px;
  }
  .ta:focus { outline: none; border-color: var(--atomizer); }
  .mono { font-family: var(--mono); }
  .actions { display: flex; align-items: center; gap: var(--space-4); }
  .hint { color: var(--text3); font-family: var(--mono); font-size: 10px; }

  .config { margin-top: var(--space-3); }
  .engine-row {
    display: grid;
    grid-template-columns: 80px 1fr;
    gap: var(--space-3);
    align-items: center;
    margin-bottom: var(--space-3);
  }
  .schema { padding-top: var(--space-2); }
  .err { color: var(--danger); font-family: var(--mono); font-size: 11px; }
  .loading { color: var(--text3); font-family: var(--mono); font-size: 11px; font-style: italic; }
</style>
