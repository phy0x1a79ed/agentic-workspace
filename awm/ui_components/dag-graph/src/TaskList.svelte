<!--
  TaskList — the primary "intelligent list": tasks grouped by state, with the
  runnable frontier + in-flight work surfaced first and done work collapsed at
  the bottom. Row click selects a task (drives the FocusPanel). The global root
  sentinel is shown muted and is not selectable.
-->
<script lang="ts">
  import { Tag } from '@awm/primitives';
  import { STATE_META, STATE_ORDER } from './types';
  import type { DagTask, TaskState } from './types';
  import type { DagIndex } from './graph-index';

  interface Props {
    index: DagIndex;
    selectedTaskId?: string | null;
    onSelectTask?: (taskId: string) => void;
  }
  let { index, selectedTaskId = null, onSelectTask }: Props = $props();

  interface Group { state: TaskState; tasks: DagTask[] }

  const root = $derived.by<DagTask | null>(() => {
    for (const t of index.taskById.values()) if (t.is_root) return t;
    return null;
  });

  const groups = $derived.by<Group[]>(() => {
    const byState = new Map<TaskState, DagTask[]>();
    for (const t of index.taskById.values()) {
      if (t.is_root) continue;
      const list = byState.get(t.state);
      if (list) list.push(t);
      else byState.set(t.state, [t]);
    }
    return STATE_ORDER
      .filter((s) => byState.has(s))
      .map((s) => ({ state: s, tasks: byState.get(s)! }));
  });

  // Runnable / in-flight / needs-attention groups open by default; done collapsed.
  function defaultOpen(s: TaskState): boolean {
    return s !== 'completed';
  }
  let overrides = $state<Partial<Record<TaskState, boolean>>>({});
  const isOpen = (s: TaskState) => overrides[s] ?? defaultOpen(s);
  const toggle = (s: TaskState) => { overrides[s] = !isOpen(s); };
</script>

<div class="list">
  {#if groups.length === 0}
    <p class="empty">No tasks in the plan yet.</p>
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
          {#each g.tasks as t (t.task_id)}
            <li>
              <button
                class="row"
                class:selected={t.task_id === selectedTaskId}
                type="button"
                onclick={() => onSelectTask?.(t.task_id)}
              >
                <span class="badge"><Tag tone={STATE_META[t.state].tone}>{STATE_META[t.state].label}</Tag></span>
                <span class="goal" title={t.goal}>{t.goal || '(no goal)'}</span>
                {#if t.workspace_slug || t.agent_ref}
                  <span class="sub">{t.agent_ref ?? t.workspace_slug}</span>
                {/if}
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </section>
  {/each}

  {#if root}
    <div class="root" title="the global root sentinel — all work is a prerequisite of it">
      ◇ root · <span class="rstate">{STATE_META[root.state].label}</span>
    </div>
  {/if}
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
  .goal {
    flex: 1 1 auto; min-width: 0; font-size: 12px; color: var(--text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .sub {
    flex: 0 0 auto; font-family: var(--mono); font-size: 9px; color: var(--text3);
    max-width: 38%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }

  .root {
    margin-top: var(--space-2); padding: var(--space-1) var(--space-2);
    font-family: var(--mono); font-size: 10px; color: var(--text3);
    border-top: 1px dashed var(--border);
  }
  .rstate { color: var(--text2); }
</style>
