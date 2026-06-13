<script lang="ts">
  /**
   * Presets card: save the current engine config under a name, load any
   * stored preset back into the composer, and one-click restore the
   * last-used config on page reload.
   *
   * Talks to the supervised backend's /presets surface. State here is
   * pure UI; canonical truth is in `.awm/state.db`'s config table.
   */
  import { Button, Card, Input, PanelLabel } from '@awm/primitives';
  import {
    deletePreset,
    listPresets,
    savePreset,
    type Preset,
    type PresetsResponse,
  } from '$lib/api/tts';

  interface Props {
    currentEngine: string;
    currentParams: Record<string, unknown>;
    onload: (engine: string, params: Record<string, unknown>) => Promise<void> | void;
  }
  let { currentEngine, currentParams, onload }: Props = $props();

  let presets = $state<Record<string, Preset>>({});
  let lastUsed = $state<{ engine: string; params: Record<string, unknown> } | null>(null);
  let loaded = $state(false);
  let loadError = $state<string | null>(null);
  let saveName = $state('');
  let saveError = $state<string | null>(null);
  let busyName = $state<string | null>(null);  // name of preset mid-load/-delete

  async function refresh() {
    try {
      const r: PresetsResponse = await listPresets();
      presets = r.presets;
      lastUsed = r.last_used;
      loaded = true;
      loadError = null;
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    }
  }

  $effect(() => { void refresh(); });

  // Sorted preset names: built-ins first (alpha), then user (alpha).
  const sortedNames = $derived.by(() => {
    const names = Object.keys(presets);
    return names.sort((a, b) => {
      const ba = presets[a]?.builtin ? 0 : 1;
      const bb = presets[b]?.builtin ? 0 : 1;
      if (ba !== bb) return ba - bb;
      return a.localeCompare(b);
    });
  });

  async function doLoad(name: string) {
    const p = presets[name];
    if (!p) return;
    busyName = name;
    try {
      await onload(p.engine, p.params);
    } finally {
      busyName = null;
    }
  }

  async function doLoadLastUsed() {
    if (!lastUsed) return;
    busyName = '__last__';
    try {
      await onload(lastUsed.engine, lastUsed.params);
    } finally {
      busyName = null;
    }
  }

  async function doSave() {
    const name = saveName.trim();
    if (!name || !currentEngine) return;
    saveError = null;
    try {
      await savePreset(name, currentEngine, currentParams);
      saveName = '';
      await refresh();
    } catch (err) {
      saveError = err instanceof Error ? err.message : String(err);
    }
  }

  async function doDelete(name: string) {
    busyName = name;
    try {
      await deletePreset(name);
      await refresh();
    } catch (err) {
      loadError = err instanceof Error ? err.message : String(err);
    } finally {
      busyName = null;
    }
  }

  function onSaveKey(e: KeyboardEvent) {
    if (e.key === 'Enter') { e.preventDefault(); void doSave(); }
  }

  // "Restore last config" is only meaningful when last-used differs from
  // the composer's current selection — otherwise it's a no-op button.
  const lastUsedDiffers = $derived.by(() => {
    if (!lastUsed) return false;
    if (lastUsed.engine !== currentEngine) return true;
    const a = lastUsed.params, b = currentParams;
    const ka = Object.keys(a), kb = Object.keys(b);
    if (ka.length !== kb.length) return true;
    return ka.some((k) => !Object.is(a[k], b[k]));
  });
</script>

<Card rail="atomizer">
  <div class="panel">
    <header class="head">
      <PanelLabel tone="atomizer">presets</PanelLabel>
      <span class="hint mono">save / load engine config</span>
    </header>

    {#if lastUsed && lastUsedDiffers}
      <Button
        kind="ghost"
        size="md"
        onclick={() => void doLoadLastUsed()}
        disabled={busyName !== null}
      >↺ restore last config — {lastUsed.engine}</Button>
    {/if}

    {#if loadError}
      <div class="err mono">{loadError}</div>
    {:else if !loaded}
      <div class="loading mono">loading presets…</div>
    {:else if sortedNames.length === 0}
      <div class="empty mono">no presets yet — save the current config below</div>
    {:else}
      <ul class="list">
        {#each sortedNames as name (name)}
          {@const p = presets[name]}
          <li class="row">
            <span class="name mono">{name}</span>
            {#if p.builtin}<span class="badge mono">built-in</span>{/if}
            <span class="engine mono">{p.engine}</span>
            <span class="spacer"></span>
            <Button
              kind="ghost"
              size="sm"
              onclick={() => void doLoad(name)}
              disabled={busyName !== null}
            >load</Button>
            {#if !p.builtin}
              <Button
                kind="danger"
                size="sm"
                onclick={() => void doDelete(name)}
                disabled={busyName !== null}
              >del</Button>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    <div class="save-row">
      <Input
        bind:value={saveName}
        placeholder="name (e.g. chelly, my-narrator)"
        onkeydown={onSaveKey}
      />
      <Button
        kind="primary"
        size="md"
        onclick={() => void doSave()}
        disabled={!saveName.trim() || !currentEngine}
      >save</Button>
    </div>
    {#if saveError}
      <div class="err mono">{saveError}</div>
    {/if}
  </div>
</Card>

<style>
  .panel { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-2) 0; }
  .head { display: flex; align-items: baseline; justify-content: space-between; }
  .hint { font-size: 10px; color: var(--text3); letter-spacing: 0.5px; }
  .mono { font-family: var(--mono); }

  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .row {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: 4px 6px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 3px);
    font-size: 11px;
  }
  .name { color: var(--text); font-weight: 500; }
  .engine { color: var(--text3); font-size: 10px; }
  .badge {
    font-size: 9px;
    color: var(--atomizer);
    border: 1px solid color-mix(in oklab, var(--atomizer) 50%, transparent);
    padding: 1px 5px;
    border-radius: 999px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
  }
  .spacer { flex: 1 1 auto; }

  .save-row { display: grid; grid-template-columns: 1fr auto; gap: var(--space-2); align-items: center; }

  .empty, .loading { color: var(--text3); font-size: 11px; font-style: italic; padding: var(--space-2) 0; }
  .err { color: var(--danger); font-size: 11px; }
</style>
