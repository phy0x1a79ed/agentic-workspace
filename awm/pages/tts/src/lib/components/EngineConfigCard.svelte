<script lang="ts">
  /**
   * Engine config card: a self-contained projection of the TTS service's
   * engine registry. On mount it fetches `listEngines`; the card has three
   * states:
   *   loading — request in flight (disabled placeholder)
   *   loaded  — engine dropdown + dynamic config form
   *   down    — fetch failed; the service is unreachable, so the card knows
   *             nothing about engines/fields. Disabled empty shell with a
   *             "TTS service unavailable" message — never falls back to
   *             hardcoded engine knowledge.
   */
  import { Card, CollapsibleSection, PanelLabel, Select } from '@awm/primitives';
  import { DynamicConfigForm } from '@awm/settings';
  import { listEngines, type EngineRegistry } from '$lib/api/tts';

  interface Props {
    selected?: string;
    params?: Record<string, unknown>;
    onengine?: (engine: string, params: Record<string, unknown>) => Promise<void> | void;
  }
  let {
    selected = $bindable(''),
    params = $bindable({}),
    onengine,
  }: Props = $props();

  type CardState = 'loading' | 'loaded' | 'down';
  let cardState = $state<CardState>('loading');
  let engines = $state<EngineRegistry | null>(null);
  let configOpen = $state(true);

  $effect(() => {
    listEngines()
      .then((reg) => {
        engines = reg;
        cardState = 'loaded';
        const ids = Object.keys(reg);
        // Seed the shared selection from defaults only if nothing is set yet.
        // Guard with `!selected` so a preset-load race isn't overwritten.
        if (ids.length && !selected) {
          selected = ids[0];
          params = { ...(reg[selected]?.defaults ?? {}) };
        }
      })
      .catch(() => {
        cardState = 'down';
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
</script>

<Card rail="manager">
  <div class="config">
    <CollapsibleSection label="engine config" rail="plain" bind:open={configOpen}>
      {#if cardState === 'loading'}
        <div class="engine-row">
          <span class="lbl"><PanelLabel>engine</PanelLabel></span>
          <Select value="" options={[]} disabled placeholder="loading…" />
        </div>
        <div class="loading mono">loading engines…</div>
      {:else if cardState === 'down'}
        <div class="engine-row">
          <span class="lbl"><PanelLabel>engine</PanelLabel></span>
          <Select value="" options={[]} disabled placeholder="unavailable" />
        </div>
        <div class="down mono">TTS service unavailable</div>
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
</Card>

<style>
  .config { padding: var(--space-2) 0; }
  .engine-row {
    display: grid;
    grid-template-columns: 80px 1fr;
    gap: var(--space-3);
    align-items: center;
    margin-bottom: var(--space-3);
  }
  .schema { padding-top: var(--space-2); }
  .mono { font-family: var(--mono); }
  .loading { color: var(--text3); font-size: 11px; font-style: italic; }
  .down { color: var(--text3); font-size: 11px; font-style: italic; }
</style>
