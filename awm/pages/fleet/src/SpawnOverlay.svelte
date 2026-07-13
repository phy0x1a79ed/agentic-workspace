<script lang="ts">
  /**
   * New-agent overlay. Two spawn modes:
   *
   *   · **scope** (default) — land the agent in an awm scope's worktree
   *     (projects/<project>/<scope>/) with its .awm/context.md + workspace MCP.
   *     The combo is a free-text `project/scope` field autocompleted from the
   *     real scope list (list_scopes): pick an existing scope, or type a new one
   *     (spawn_scoped provisions it). Submits {project, scope}.
   *   · **directory** — the adhoc path: a plain agent at any bare directory,
   *     autocompleted from recent roster cwds. Submits {cwd}.
   *
   * harness · model · effort are shared, prefilled from the last-used defaults.
   * Submit launches an idle tmux agent and hands its tmux session back to attach.
   */
  import { onMount } from 'svelte';
  import {
    spawnAgent, spawnScoped, listScopes, saveSpawnDefaults, type SpawnResult,
  } from './lib/api';
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

  // A last-used value that already looks like `project/scope` (no leading slash,
  // exactly one segment separator) implies the scope mode was used last.
  const looksLikeScope = (v: string): boolean =>
    !!v && !v.startsWith('/') && v.split('/').filter(Boolean).length === 2;

  let mode = $state<'scope' | 'dir'>(looksLikeScope(defaults.scope || '') ? 'scope' : 'dir');
  let harness = $state(defaults.harness || 'claude');
  let model = $state(defaults.model || 'sonnet');
  let effort = $state(defaults.effort || 'medium');
  // In scope mode this holds `project/scope`; in dir mode a bare path. Seeded
  // from the last-used default only when it matches the current mode.
  let scopeRef = $state(looksLikeScope(defaults.scope || '') ? defaults.scope : '');
  let cwd = $state(looksLikeScope(defaults.scope || '') ? '' : (defaults.scope || ''));
  let scopeSuggestions = $state<string[]>([]);
  let busy = $state(false);
  let error = $state<string | null>(null);

  onMount(() => {
    // Populate the scope picker from the real scope list (best-effort).
    listScopes()
      .then((r) => { scopeSuggestions = r.scopes.map((s) => `${s.project}/${s.scope}`); })
      .catch(() => { scopeSuggestions = []; });
  });

  /** Split a free-text `project/scope` on the first slash. */
  function parseScopeRef(ref: string): { project: string; scope: string } | null {
    const t = ref.trim().replace(/^\/+|\/+$/g, '');
    const i = t.indexOf('/');
    if (i <= 0 || i >= t.length - 1) return null;
    return { project: t.slice(0, i), scope: t.slice(i + 1) };
  }

  async function submit(): Promise<void> {
    error = null;
    busy = true;
    try {
      let res: SpawnResult;
      let lastUsed: string;
      if (mode === 'scope') {
        const parsed = parseScopeRef(scopeRef);
        if (!parsed) { error = 'enter a scope as project/scope'; busy = false; return; }
        const base = { project: parsed.project, scope: parsed.scope, harness };
        res = await spawnScoped(
          harness === 'claude' ? { ...base, model, effort } : base);
        lastUsed = `${parsed.project}/${parsed.scope}`;
      } else {
        if (!cwd.trim()) { error = 'pick a directory'; busy = false; return; }
        const base = { cwd: cwd.trim(), harness };
        res = await spawnAgent(
          harness === 'claude' ? { ...base, model, effort } : base);
        lastUsed = cwd.trim();
      }
      // Remember last-used so next open prefills these.
      void saveSpawnDefaults({ harness, model, effort, scope: lastUsed });
      onSpawned(res);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }
</script>

<FleetSheet title="New agent" onClose={onClose}>
  <div class="seg" role="tablist">
    <button class="seg-b" class:on={mode === 'scope'} role="tab"
            aria-selected={mode === 'scope'} onclick={() => (mode = 'scope')}>scope</button>
    <button class="seg-b" class:on={mode === 'dir'} role="tab"
            aria-selected={mode === 'dir'} onclick={() => (mode = 'dir')}>directory</button>
  </div>

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

  {#if mode === 'scope'}
    <div class="fld">
      <span class="fld-l">scope</span>
      <ScopeCombo bind:value={scopeRef} suggestions={scopeSuggestions}
                  placeholder="project/scope" />
      <span class="fld-hint">pick a scope, or type a new one to create it</span>
    </div>
  {:else}
    <div class="fld">
      <span class="fld-l">directory</span>
      <ScopeCombo bind:value={cwd} suggestions={cwdSuggestions} />
    </div>
  {/if}

  {#if error}<p class="ov-err">{error}</p>{/if}

  {#snippet footer()}
    <button class="btn-ghost" onclick={onClose}>cancel</button>
    <button class="btn-go" onclick={submit} disabled={busy}>
      {busy ? 'launching…' : 'launch'}
    </button>
  {/snippet}
</FleetSheet>

<style>
  .seg { display: flex; gap: 4px; padding: 3px;
    background: var(--surface, #111); border: 1px solid var(--border, #333);
    border-radius: 22px; }
  .seg-b {
    flex: 1; padding: 7px 12px; border-radius: 18px;
    background: none; border: none; cursor: pointer;
    color: var(--text2, #aaa); font-size: 0.82rem;
  }
  .seg-b.on { background: var(--accent, #4b8bff); color: #fff; }
  .fld { display: flex; flex-direction: column; gap: 4px; }
  .fld-l { font-size: 0.72rem; color: var(--text3, #999); text-transform: uppercase; letter-spacing: 0.04em; }
  .fld-hint { font-size: 0.72rem; color: var(--text3, #888); }
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
