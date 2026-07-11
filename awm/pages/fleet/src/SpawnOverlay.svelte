<script lang="ts">
  /**
   * New-agent overlay: four fields — harness · model · effort · scope — all
   * dropdowns (scope is a typable combo allowing arbitrary paths), prefilled
   * from the last-used spawn defaults. Submit launches an idle plain-tmux agent
   * and hands its tmux session back so the caller can attach it.
   */
  import { spawnAgent, saveSpawnDefaults, type SpawnResult } from './lib/api';
  import ScopeCombo from './ScopeCombo.svelte';
  import FleetSheet from './FleetSheet.svelte';
  import type { FleetConfig } from './lib/types';

  interface Props {
    defaults: FleetConfig['spawn_defaults'];
    cwdSuggestions: string[];
    onClose: () => void;
    onSpawned: (r: SpawnResult) => void;
  }
  let { defaults, cwdSuggestions, onClose, onSpawned }: Props = $props();

  const HARNESSES = ['claude', 'opencode'];
  const CLAUDE_MODELS = ['opus', 'sonnet', 'haiku', 'fable'];
  const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];

  let harness = $state(defaults.harness || 'claude');
  let model = $state(defaults.model || 'sonnet');
  let effort = $state(defaults.effort || 'medium');
  let cwd = $state(defaults.scope || '');
  let busy = $state(false);
  let error = $state<string | null>(null);

  async function submit(): Promise<void> {
    error = null;
    if (!cwd.trim()) { error = 'pick a directory'; return; }
    busy = true;
    try {
      const args =
        harness === 'claude'
          ? { cwd: cwd.trim(), harness, model, effort }
          : { cwd: cwd.trim(), harness };
      const res = await spawnAgent(args);
      // Remember last-used so next open prefills these.
      void saveSpawnDefaults({ harness, model, effort, scope: cwd.trim() });
      onSpawned(res);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }
</script>

<FleetSheet title="New agent" onClose={onClose}>
  <label class="fld">
    <span class="fld-l">harness</span>
    <select bind:value={harness}>
      {#each HARNESSES as h (h)}<option value={h}>{h}</option>{/each}
    </select>
  </label>

  {#if harness === 'claude'}
    <label class="fld">
      <span class="fld-l">model</span>
      <select bind:value={model}>
        {#each CLAUDE_MODELS as m (m)}<option value={m}>{m}</option>{/each}
      </select>
    </label>
    <label class="fld">
      <span class="fld-l">effort</span>
      <select bind:value={effort}>
        {#each EFFORTS as e (e)}<option value={e}>{e}</option>{/each}
      </select>
    </label>
  {:else}
    <p class="fld-note">opencode picks its model in-app.</p>
  {/if}

  <div class="fld">
    <span class="fld-l">scope / cwd</span>
    <ScopeCombo bind:value={cwd} suggestions={cwdSuggestions} />
  </div>

  {#if error}<p class="ov-err">{error}</p>{/if}

  {#snippet footer()}
    <button class="btn-ghost" onclick={onClose}>cancel</button>
    <button class="btn-go" onclick={submit} disabled={busy}>
      {busy ? 'launching…' : 'launch'}
    </button>
  {/snippet}
</FleetSheet>

<style>
  .fld { display: flex; flex-direction: column; gap: 4px; }
  .fld-l { font-size: 0.72rem; color: var(--text3, #999); text-transform: uppercase; letter-spacing: 0.04em; }
  .fld-note { font-size: 0.8rem; color: var(--text3, #888); margin: 0; }
  select {
    padding: 10px 12px;
    background: var(--surface2, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 8px);
    color: var(--text, #eee);
    font-size: 16px;
  }
  .ov-err { color: var(--danger, #e06c75); font-size: 0.82rem; margin: 0; }
  .btn-ghost, .btn-go {
    padding: 10px 18px; border-radius: 22px; cursor: pointer; font-size: 0.9rem;
    border: 1px solid var(--border, #333);
  }
  .btn-ghost { background: none; color: var(--text2, #aaa); }
  .btn-go { background: var(--accent, #4b8bff); border-color: transparent; color: #fff; }
  .btn-go:disabled { opacity: 0.5; }
</style>
