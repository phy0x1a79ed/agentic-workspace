<script lang="ts">
  /**
   * Config unit: composes the engine config card and the preset card. It owns
   * no fetch and no up/down logic — each card is a self-contained projection
   * of the service. This wrapper only threads the shared `selected`/`params`
   * through (two-way, so writes propagate back to the page) and forwards the
   * engine-change / preset-load callbacks.
   */
  import EngineConfigCard from '$lib/components/EngineConfigCard.svelte';
  import PresetCard from '$lib/components/PresetCard.svelte';

  interface Props {
    selected?: string;
    params?: Record<string, unknown>;
    onengine?: (engine: string, params: Record<string, unknown>) => Promise<void> | void;
    onpresetload?: (engine: string, params: Record<string, unknown>) => Promise<void> | void;
  }
  let {
    selected = $bindable(''),
    params = $bindable({}),
    onengine,
    onpresetload,
  }: Props = $props();
</script>

<div class="config-unit">
  <EngineConfigCard bind:selected bind:params {onengine} />
  <PresetCard currentEngine={selected} currentParams={params} onload={onpresetload} />
</div>

<style>
  .config-unit { display: flex; flex-direction: column; gap: var(--space-3); margin-top: var(--space-3); }
</style>
