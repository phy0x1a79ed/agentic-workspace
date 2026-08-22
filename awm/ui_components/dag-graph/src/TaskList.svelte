<!--
  TaskList — the primary "intelligent list": tasks grouped by state, with the
  runnable frontier + in-flight work surfaced first and done work collapsed at
  the bottom. Row click selects a task (drives the FocusPanel). The global root
  sentinel is shown muted and is not selectable.

  Pinned above the state groups is the ATTENTION block — the same signal the old
  top strip carried, now folded into the list: "needs you" (wants steering /
  attached) and "broken" (failed / abandoned), derived from `deriveAttention`
  off the same poll. A task legitimately appears both here and in its state
  group — the pinned block is a shortcut to the stalest work needing a human.
-->
<script lang="ts">
  import { Tag } from '@awm/primitives';
  import { STATE_META, STATE_ORDER } from './types';
  import type { DagTask, TaskState } from './types';
  import type { DagIndex } from './graph-index';
  import { deriveAttention } from './attention';

  interface Props {
    index: DagIndex;
    selectedTaskId?: string | null;
    onSelectTask?: (taskId: string) => void;
    /** Free-text filter: matched (case-insensitive) against title, tags, and goal. */
    query?: string;
    /** Re-derive tick: the page bumps this each poll so attention ages refresh. */
    nowMs?: number;
  }
  let { index, selectedTaskId = null, onSelectTask, query = '', nowMs }: Props = $props();

  interface Group { state: TaskState; tasks: DagTask[] }

  // Client-side free-text match over the already-fetched tasks — title, tags,
  // and goal. Empty query matches everything.
  function matches(t: DagTask, q: string): boolean {
    if (!q) return true;
    const hay = `${t.title ?? ''} ${(t.tags ?? []).join(' ')} ${t.goal ?? ''}`.toLowerCase();
    return hay.includes(q);
  }

  const q = $derived(query.trim().toLowerCase());

  const groups = $derived.by<Group[]>(() => {
    const byState = new Map<TaskState, DagTask[]>();
    for (const t of index.taskById.values()) {
      if (t.is_root) continue;
      if (!matches(t, q)) continue;
      const list = byState.get(t.state);
      if (list) list.push(t);
      else byState.set(t.state, [t]);
    }
    return STATE_ORDER
      .filter((s) => byState.has(s))
      .map((s) => ({ state: s, tasks: byState.get(s)! }));
  });

  // Attention groups, pinned above the state groups. `deriveAttention` returns
  // entries oldest-first; map each back to its task (skipping any that the query
  // filters out) so the pinned rows render identically to the state-group rows.
  const attention = $derived(deriveAttention([...index.taskById.values()], nowMs ?? Date.now()));
  function toTasks(entries: { taskId: string }[]): DagTask[] {
    const out: DagTask[] = [];
    for (const e of entries) {
      const t = index.taskById.get(e.taskId);
      if (t && matches(t, q)) out.push(t);
    }
    return out;
  }
  const wants = $derived(toTasks(attention.wants));
  const broken = $derived(toTasks(attention.broken));

  // Runnable / in-flight / needs-attention groups open by default; done collapsed.
  function defaultOpen(s: TaskState): boolean {
    return s !== 'completed';
  }
  let overrides = $state<Partial<Record<TaskState, boolean>>>({});
  const isOpen = (s: TaskState) => overrides[s] ?? defaultOpen(s);
  const toggle = (s: TaskState) => { overrides[s] = !isOpen(s); };

  // Attention groups open by default (they're the priority signal).
  let attnOverrides = $state<{ wants?: boolean; broken?: boolean }>({});
  const attnOpen = (k: 'wants' | 'broken') => attnOverrides[k] ?? true;
  const toggleAttn = (k: 'wants' | 'broken') => { attnOverrides[k] = !attnOpen(k); };
</script>

{#snippet taskRow(t: DagTask)}
  <li>
    <button
      class="row"
      class:selected={t.task_id === selectedTaskId}
      type="button"
      onclick={() => onSelectTask?.(t.task_id)}
    >
      <span class="badge"><Tag tone={STATE_META[t.state].tone}>{STATE_META[t.state].label}</Tag></span>
      {#if t.attached}<span class="mk att" title="attached — you are steering">◉</span>{/if}
      {#if t.steer_requested && !t.attached}<span class="mk want" title="wants steering">◆</span>{/if}
      {#if t.paused}<span class="mk pau" title="paused">⏸</span>{/if}
      <span class="goal" title={t.goal}>{t.title || t.goal || '(no goal)'}</span>
      {#each (t.tags ?? []).slice(0, 3) as tag (tag)}
        <span class="chip">{tag}</span>
      {/each}
      {#if t.workspace_slug || t.agent_ref}
        <span class="sub">{t.mode ? `${t.mode}·` : ''}{t.agent_ref ?? t.workspace_slug}</span>
      {/if}
    </button>
  </li>
{/snippet}

<div class="list">
  {#if groups.length === 0 && wants.length === 0 && broken.length === 0}
    <p class="empty">No tasks in the plan yet.</p>
  {/if}

  <!-- Attention block: pinned above the state groups. -->
  {#if wants.length}
    <section class="group attn wants">
      <button class="ghead" type="button" aria-expanded={attnOpen('wants')} onclick={() => toggleAttn('wants')}>
        <span class="chev" class:open={attnOpen('wants')}>▸</span>
        <span class="dot"></span>
        <span class="glabel">needs you</span>
        <span class="gcount">{wants.length}</span>
      </button>
      {#if attnOpen('wants')}
        <ul class="rows">
          {#each wants as t (t.task_id)}{@render taskRow(t)}{/each}
        </ul>
      {/if}
    </section>
  {/if}
  {#if broken.length}
    <section class="group attn broken">
      <button class="ghead" type="button" aria-expanded={attnOpen('broken')} onclick={() => toggleAttn('broken')}>
        <span class="chev" class:open={attnOpen('broken')}>▸</span>
        <span class="dot"></span>
        <span class="glabel">broken</span>
        <span class="gcount">{broken.length}</span>
      </button>
      {#if attnOpen('broken')}
        <ul class="rows">
          {#each broken as t (t.task_id)}{@render taskRow(t)}{/each}
        </ul>
      {/if}
    </section>
  {/if}

  {#each groups as g (g.state)}
    <section class="group">
      <button
        class="ghead"
        type="button"
        aria-expanded={isOpen(g.state)}
        onclick={() => toggle(g.state)}
      >
        <span class="chev" class:open={isOpen(g.state)}>▸</span>
        <span class="glabel">{STATE_META[g.state].label}</span>
        <span class="gcount">{g.tasks.length}</span>
      </button>

      {#if isOpen(g.state)}
        <ul class="rows">
          {#each g.tasks as t (t.task_id)}{@render taskRow(t)}{/each}
        </ul>
      {/if}
    </section>
  {/each}
</div>

<style>
  .list { display: flex; flex-direction: column; gap: var(--space-2); min-width: 0; }
  .empty { color: var(--text3); font-size: 12px; padding: var(--space-3); }

  .group { display: flex; flex-direction: column; }
  .ghead {
    display: flex; align-items: center; gap: var(--space-2);
    background: transparent; border: 0; cursor: pointer;
    padding: var(--space-1) 0; color: inherit; text-align: left;
  }
  .chev {
    font-family: var(--mono); font-size: 11px; color: var(--text3);
    transition: transform 160ms var(--ease-mech);
  }
  .chev.open { transform: rotate(90deg); color: var(--text2); }
  .glabel {
    font-family: var(--mono); font-size: 9px; letter-spacing: 2px;
    text-transform: uppercase; color: var(--text2);
  }
  .gcount {
    font-family: var(--mono); font-size: 9px; color: var(--text3);
    background: var(--surface3); border-radius: var(--radius-md);
    padding: 0 var(--space-2);
  }

  /* Attention groups: a colored dot + accented label distinguishes the pinned
     block from the state groups. */
  .attn .dot { flex: 0 0 auto; width: 6px; height: 6px; border-radius: 50%; }
  .attn.wants .dot { background: var(--warn); }
  .attn.broken .dot { background: var(--danger); }
  .attn.wants .glabel { color: var(--warn); }
  .attn.broken .glabel { color: var(--danger); }

  .rows { list-style: none; margin: 0; padding: 0 0 0 var(--space-3);
          display: flex; flex-direction: column; gap: 2px; }
  .row {
    width: 100%; display: flex; align-items: center; gap: var(--space-2);
    background: transparent; border: 1px solid transparent;
    border-radius: var(--radius-md);
    padding: var(--space-1) var(--space-2); cursor: pointer;
    color: inherit; text-align: left;
    transition: background 120ms var(--ease-mech), border-color 120ms var(--ease-mech);
  }
  .row:hover {
    background: color-mix(in oklab, var(--atomizer) 8%, var(--surface2));
    border-color: var(--border);
  }
  .row.selected {
    background: color-mix(in oklab, var(--atomizer) 16%, var(--surface2));
    border-color: color-mix(in oklab, var(--atomizer) 45%, var(--border));
  }
  .badge { flex: 0 0 auto; }
  .mk { flex: 0 0 auto; font-size: 10px; line-height: 1; }
  .mk.att { color: var(--atomizer); }
  .mk.want { color: var(--warn); }
  .mk.pau { color: var(--warn); }
  .goal {
    flex: 1 1 auto; min-width: 0; font-size: 12px; color: var(--text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .chip {
    flex: 0 0 auto; font-size: 9px; color: var(--text2); background: var(--surface3);
    border-radius: var(--radius-md); padding: 0 var(--space-1);
    max-width: 90px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .sub {
    flex: 0 0 auto; font-family: var(--mono); font-size: 9px; color: var(--text3);
    max-width: 38%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
</style>
