<!--
  FocusPanel — the selected task's immediate neighbourhood. Replaces the canvas:
  rather than draw the whole DAG, show one task's dependencies (upstream / what
  it NEEDS) and dependents (downstream / what's NEXT once it delivers), each row
  labelled by the connecting contract + a delivered/pending mark. Clicking a
  neighbour re-selects it, so the user walks the DAG one hop at a time.
-->
<script lang="ts">
  import { marked } from 'marked';
  import DOMPurify from 'dompurify';
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

  function taskOf(id: string): DagTask | undefined {
    return index.taskById.get(id);
  }

  const selected = $derived(
    selectedTaskId ? index.taskById.get(selectedTaskId) ?? null : null,
  );
  // Hide the global root sentinel from the neighbourhood — it's bookkeeping, not
  // real work, so it never reads as a meaningful dependency/dependent.
  const deps = $derived<NeighborRef[]>(
    (selectedTaskId ? upstream(index, selectedTaskId) : []).filter(
      (r) => !taskOf(r.taskId)?.is_root,
    ),
  );
  const dependents = $derived<NeighborRef[]>(
    (selectedTaskId ? downstream(index, selectedTaskId) : []).filter(
      (r) => !taskOf(r.taskId)?.is_root,
    ),
  );

  // The task's goal IS the starting prompt — render it as sanitized markdown in
  // a clearly-marked, wrapping block (goals are authored in markdown).
  const promptHtml = $derived.by<string>(() => {
    const src = selected?.goal?.trim();
    if (!src) return '<em>(no goal)</em>';
    return DOMPurify.sanitize(marked.parse(src, { async: false }) as string);
  });
</script>

{#if !selected}
  <div class="focus empty">
    <p>Select a task to see what it needs and what it feeds.</p>
  </div>
{:else}
  <div class="focus">
    <header class="sel">
      <div class="selhead">
        <Tag tone={STATE_META[selected.state].tone}>{STATE_META[selected.state].label}</Tag>
        {#if selected.agent_ref}<span class="meta">{selected.agent_ref}</span>{/if}
      </div>
      <PanelLabel>Prompt</PanelLabel>
      <!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized via DOMPurify -->
      <div class="prompt">{@html promptHtml}</div>
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

  .sel { display: flex; flex-direction: column; gap: var(--space-2); }
  .selhead { display: flex; align-items: center; gap: var(--space-2); }
  .meta { font-family: var(--mono); font-size: 9px; color: var(--text3); }

  .prompt {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: var(--radius-md); padding: var(--space-2) var(--space-3);
    font-size: 13px; line-height: 1.5; color: var(--text);
    white-space: normal; word-break: break-word; overflow-wrap: anywhere;
    max-height: 340px; overflow-y: auto;
  }
  .prompt :global(> :first-child) { margin-top: 0; }
  .prompt :global(> :last-child) { margin-bottom: 0; }
  .prompt :global(p) { margin: 0 0 var(--space-2); }
  .prompt :global(ul), .prompt :global(ol) { margin: 0 0 var(--space-2); padding-left: 1.4em; }
  .prompt :global(pre) {
    white-space: pre-wrap; overflow-x: auto; background: var(--surface3);
    padding: var(--space-2); border-radius: var(--radius-sm);
  }
  .prompt :global(code) { font-family: var(--mono); font-size: 12px; }
  .prompt :global(h1), .prompt :global(h2), .prompt :global(h3) {
    margin: var(--space-2) 0 var(--space-1); font-size: 13px; font-weight: 600;
  }

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
