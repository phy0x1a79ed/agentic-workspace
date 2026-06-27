<!--
  FocusPanel — the selected task's immediate neighbourhood. Replaces the canvas:
  rather than draw the whole DAG, show one task's dependencies (upstream / what
  it NEEDS) and dependents (downstream / what's NEXT once it delivers), each row
  labelled by the connecting contract + a delivered/pending mark. Clicking a
  neighbour re-selects it, so the user walks the DAG one hop at a time.
-->
<script lang="ts">
  import { Tag, PanelLabel } from '@awm/primitives';
  import { STATE_META } from './types';
  import type { DagTask } from './types';
  import type { DagIndex, NeighborRef } from './graph-index';
  import { upstream, downstream } from './graph-index';

  interface Props {
    index: DagIndex;
    selectedTaskId?: string | null;
    onSelectTask?: (taskId: string) => void;
  }
  let { index, selectedTaskId = null, onSelectTask }: Props = $props();

  const selected = $derived(
    selectedTaskId ? index.taskById.get(selectedTaskId) ?? null : null,
  );
  const deps = $derived<NeighborRef[]>(
    selectedTaskId ? upstream(index, selectedTaskId) : [],
  );
  const dependents = $derived<NeighborRef[]>(
    selectedTaskId ? downstream(index, selectedTaskId) : [],
  );

  function taskOf(id: string): DagTask | undefined {
    return index.taskById.get(id);
  }
</script>

{#if !selected}
  <div class="focus empty">
    <p>Select a task to see what it needs and what it feeds.</p>
  </div>
{:else}
  <div class="focus">
    <header class="sel">
      <Tag tone={STATE_META[selected.state].tone}>{STATE_META[selected.state].label}</Tag>
      <h3 title={selected.goal}>{selected.goal || '(no goal)'}</h3>
      {#if selected.is_root}
        <p class="note">The global root sentinel — every top-level deliverable is a prerequisite of it.</p>
      {/if}
      {#if selected.agent_ref}
        <p class="meta">{selected.agent_ref}</p>
      {/if}
    </header>

    {#snippet side(title: string, hint: string, refs: NeighborRef[])}
      <section class="col">
        <div class="collbl"><PanelLabel>{title}</PanelLabel><span class="hint">{hint}</span></div>
        {#if refs.length === 0}
          <p class="none">none</p>
        {:else}
          <ul>
            {#each refs as r (r.contractId + r.taskId)}
              {@const nt = taskOf(r.taskId)}
              <li>
                <button class="nbr" type="button" onclick={() => onSelectTask?.(r.taskId)}>
                  <span class="mark" class:done={r.delivered} title={r.delivered ? 'delivered' : 'pending'}>
                    {r.delivered ? '▣' : '▢'}
                  </span>
                  <span class="ctr" title="contract">{r.contractName}</span>
                  {#if nt}
                    <span class="ntag"><Tag tone={STATE_META[nt.state].tone}>{STATE_META[nt.state].label}</Tag></span>
                    <span class="ngoal" title={nt.goal}>{nt.goal || '(no goal)'}</span>
                  {:else}
                    <span class="ngoal off">{r.taskId.slice(0, 8)}…</span>
                  {/if}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    {/snippet}

    <div class="cols">
      {@render side('Dependencies', 'needs', deps)}
      {@render side('Dependents', "what's next", dependents)}
    </div>
  </div>
{/if}

<style>
  .focus { display: flex; flex-direction: column; gap: var(--space-3); min-width: 0; }
  .focus.empty p { color: var(--text3); font-size: 12px; }

  .sel { display: flex; flex-direction: column; gap: var(--space-1); }
  .sel h3 {
    margin: 0; font-size: 14px; font-weight: 600; color: var(--text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .sel .note { margin: 0; font-size: 11px; color: var(--text3); }
  .sel .meta { margin: 0; font-family: var(--mono); font-size: 9px; color: var(--text3); }

  .cols { display: flex; gap: var(--space-4); align-items: flex-start; }
  .col { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .collbl { display: flex; align-items: baseline; gap: var(--space-2); }
  .hint { font-size: 9px; color: var(--text3); }
  .none { color: var(--text3); font-size: 11px; font-style: italic; margin: 0; padding: var(--space-1) 0; }

  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .nbr {
    width: 100%; display: flex; align-items: center; gap: var(--space-2);
    background: transparent; border: 1px solid transparent; border-radius: var(--radius-md);
    padding: var(--space-1) var(--space-2); cursor: pointer; color: inherit; text-align: left;
    transition: background 120ms var(--ease-mech), border-color 120ms var(--ease-mech);
  }
  .nbr:hover {
    background: color-mix(in oklab, var(--atomizer) 8%, var(--surface2));
    border-color: var(--border);
  }
  .mark { flex: 0 0 auto; font-size: 11px; color: var(--text3); }
  .mark.done { color: var(--ok); }
  .ctr {
    flex: 0 0 auto; max-width: 45%; font-family: var(--mono); font-size: 10px;
    color: var(--atomizer); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .ntag { flex: 0 0 auto; }
  .ngoal {
    flex: 1 1 auto; min-width: 0; font-size: 11px; color: var(--text2);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .ngoal.off { font-family: var(--mono); color: var(--text3); }
</style>
