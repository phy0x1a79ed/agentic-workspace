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
  import type { DagTask, TaskDetail } from './types';
  import type { DagIndex, NeighborRef } from './graph-index';
  import { upstream, downstream } from './graph-index';
  import SteeringChip from './SteeringChip.svelte';

  interface Props {
    index: DagIndex;
    selectedTaskId?: string | null;
    onSelectTask?: (taskId: string) => void;
    /** Persist a user edit to the task's title (the page wires the client verb). */
    onSetTitle?: (taskId: string, title: string) => void;
    /** Persist a user edit to the task's tags. */
    onSetTags?: (taskId: string, tags: string[]) => void;
    /** Toggle the task's sticky pause (by unit slug; task_id for the durable mirror). */
    onSetPaused?: (slug: string, paused: boolean, taskId: string) => void;
    /**
     * The on-selection heavier read for the SELECTED task (objective text, plan
     * ref, attempt memories). The page owns the fetch (dag-graph stays
     * presentational) and hands it down; null while it loads or for no selection.
     */
    detail?: TaskDetail | null;
    /** Cancel a non-terminal, non-root task (consumers re-plan). */
    onCancel?: (taskId: string) => void;
    /** Retry a failed/abandoned task (fresh budget, retained work). */
    onRetry?: (taskId: string) => void;
    /** Busy-until-poll: disable the lifecycle buttons after a click. */
    lifecycleBusy?: boolean;
  }
  let {
    index, selectedTaskId = null, onSelectTask,
    onSetTitle, onSetTags, onSetPaused,
    detail = null, onCancel, onRetry, lifecycleBusy = false,
  }: Props = $props();

  function taskOf(id: string): DagTask | undefined {
    return index.taskById.get(id);
  }

  const selected = $derived(
    selectedTaskId ? index.taskById.get(selectedTaskId) ?? null : null,
  );

  // --- Editable title + tags -------------------------------------------------
  // Local drafts reset only when the SELECTION changes (not on every poll), so a
  // background refresh never clobbers what the user is typing.
  let titleDraft = $state('');
  let tagDraft = $state('');
  let lastSelId: string | null = null;
  $effect(() => {
    const id = selected?.task_id ?? null;
    if (id !== lastSelId) {
      lastSelId = id;
      titleDraft = selected?.title ?? '';
      tagDraft = '';
    }
  });

  function commitTitle() {
    if (!selected) return;
    const next = titleDraft.trim();
    if (next !== (selected.title ?? '')) onSetTitle?.(selected.task_id, next);
  }
  function onTitleKey(e: KeyboardEvent) {
    if (e.key === 'Enter') { e.preventDefault(); (e.target as HTMLInputElement).blur(); }
  }
  function addTag() {
    if (!selected) return;
    const t = tagDraft.trim();
    if (!t) return;
    const cur = selected.tags ?? [];
    if (!cur.includes(t)) onSetTags?.(selected.task_id, [...cur, t]);
    tagDraft = '';
  }
  function onTagKey(e: KeyboardEvent) {
    if (e.key === 'Enter') { e.preventDefault(); addTag(); }
  }
  function removeTag(t: string) {
    if (!selected) return;
    onSetTags?.(selected.task_id, (selected.tags ?? []).filter((x) => x !== t));
  }
  function togglePause() {
    if (!selected?.workspace_slug) return;
    onSetPaused?.(selected.workspace_slug, !selected.paused, selected.task_id);
  }
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

  // The durable OBJECTIVE record — system-written, never hand-edited — rendered
  // read-only as sanitized markdown (same treatment as the goal). The detail is
  // fetched per-selection by the page; `detail` may lag the selection by a beat.
  const detailForSelected = $derived(
    detail && selected && detail.task_id === selected.task_id ? detail : null,
  );
  const objectiveHtml = $derived.by<string>(() => {
    const src = detailForSelected?.objective?.trim();
    if (!src) return '';
    return DOMPurify.sanitize(marked.parse(src, { async: false }) as string);
  });

  // Lifecycle button enablement (the backend is authoritative and rejects an
  // invalid transition; these are affordance hints):
  //   Cancel   — any non-terminal, non-root node.
  //   Retry    — only a failed/abandoned node.
  // (Decompose is still reachable as the orch_decompose verb; the button was
  //  removed from this panel.)
  const TERMINAL = new Set(['completed', 'failed', 'abandoned']);
  const isTerminal = $derived(!!selected && TERMINAL.has(selected.state));
  const canCancel = $derived(!!selected && !isTerminal && !selected.is_root);
  const canRetry = $derived(
    !!selected && (selected.state === 'failed' || selected.state === 'abandoned'),
  );

  // Inline confirm for Cancel (no browser dialog — the affordance flips in place
  // and states the downstream cost). Reset whenever the selection changes.
  let confirmingCancel = $state(false);
  $effect(() => {
    // Touch the selection so the confirm resets when the user moves on.
    void selectedTaskId;
    confirmingCancel = false;
  });
  function doCancel() {
    if (!selected) return;
    onCancel?.(selected.task_id);
    confirmingCancel = false;
  }
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
        <input
          class="title"
          type="text"
          placeholder="auto-titled — optional override"
          aria-label="task title (auto-derived; optional human override)"
          title="Auto-derived from the objective record. Editing it is an optional human override — you never have to set it."
          bind:value={titleDraft}
          onblur={commitTitle}
          onkeydown={onTitleKey}
        />
        {#if selected.workspace_slug}
          <button
            class="pause"
            type="button"
            class:on={selected.paused}
            title={selected.paused ? 'paused — supervisor frozen; click to resume' : 'running — click to pause (supervisor stays out)'}
            onclick={togglePause}
          >{selected.paused ? '⏸' : '▶'}</button>
        {/if}
      </div>

      <!-- agent info line: who is on it + steering / attention state -->
      <div class="agentline">
        {#if selected.mode}<span class="mode">{selected.mode}</span>{/if}
        {#if selected.agent_ref || selected.workspace_slug}
          <span class="meta" title="placed agent / unit slug">{selected.agent_ref ?? selected.workspace_slug}</span>
        {/if}
        <SteeringChip task={selected} />
        {#if selected.paused}<span class="flag pau" title="supervisor frozen">paused</span>{/if}
      </div>

      <!-- editable tags -->
      <div class="tags">
        {#each selected.tags ?? [] as t (t)}
          <span class="chip">
            {t}<button class="x" type="button" title="remove tag" onclick={() => removeTag(t)}>✕</button>
          </span>
        {/each}
        <input
          class="tagin"
          type="text"
          placeholder="+ tag"
          aria-label="add tag"
          bind:value={tagDraft}
          onkeydown={onTagKey}
          onblur={addTag}
        />
      </div>

      <!-- the goal (the human-authored starting prompt), labeled, as sanitized markdown -->
      <div class="goalwrap">
        <div class="collbl"><PanelLabel>Goal</PanelLabel><span class="hint">starting prompt</span></div>
        <!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized via DOMPurify -->
        <div class="prompt">{@html promptHtml}</div>
      </div>

      <!-- the durable objective record (system-written, read-only) -->
      <div class="objwrap">
        <div class="collbl"><PanelLabel>Objective</PanelLabel><span class="hint">system-written · read-only</span></div>
        {#if objectiveHtml}
          <!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized via DOMPurify -->
          <div class="prompt obj">{@html objectiveHtml}</div>
        {:else if selected.has_objective && !detailForSelected}
          <p class="none">loading…</p>
        {:else}
          <p class="none">no objective record yet — attach and steer the task to distill one.</p>
        {/if}
      </div>

      <!-- lifecycle controls: cancel / retry / decompose (busy-until-poll) -->
      <div class="lifecycle">
        {#if confirmingCancel}
          <span class="confirmcopy" role="alert">Cancel this task? Its downstream consumers will re-plan.</span>
          <button class="lc danger" type="button" disabled={lifecycleBusy} onclick={doCancel}>confirm cancel</button>
          <button class="lc" type="button" onclick={() => (confirmingCancel = false)}>keep</button>
        {:else}
          <button
            class="lc"
            type="button"
            disabled={!canCancel || lifecycleBusy}
            title={canCancel ? 'cancel this task (consumers re-plan)' : 'only a non-terminal, non-root task can be cancelled'}
            onclick={() => (confirmingCancel = true)}
          >cancel</button>
          <button
            class="lc"
            type="button"
            disabled={!canRetry || lifecycleBusy}
            title={canRetry ? 'retry with a fresh budget (keeps retained work)' : 'only a failed / abandoned task can be retried'}
            onclick={() => selected && onRetry?.(selected.task_id)}
          >retry</button>
          {#if lifecycleBusy}<span class="hint">working…</span>{/if}
        {/if}
      </div>
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
                    <span class="ngoal" title={nt.goal}>{nt.title || nt.goal || '(no goal)'}</span>
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

  .title {
    flex: 1 1 auto; min-width: 0; background: transparent; border: 1px solid transparent;
    border-radius: var(--radius-md); color: var(--text); font-size: 15px; font-weight: 600;
    padding: var(--space-1) var(--space-2); font-family: inherit;
  }
  .title::placeholder { color: var(--text3); font-weight: 400; font-style: italic; }
  .title:hover { border-color: var(--border); }
  .title:focus { outline: none; border-color: var(--atomizer); background: var(--surface2); }

  .pause {
    flex: 0 0 auto; background: var(--surface2); border: 1px solid var(--border);
    border-radius: var(--radius-md); color: var(--text2); cursor: pointer;
    font-size: 11px; line-height: 1; padding: var(--space-1) var(--space-2);
  }
  .pause:hover { color: var(--text); border-color: var(--atomizer); }
  .pause.on { color: var(--warn); border-color: color-mix(in oklab, var(--warn) 50%, var(--border)); }

  .agentline { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .mode {
    font-family: var(--mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase;
    color: var(--text2); background: var(--surface3); border-radius: var(--radius-md);
    padding: 0 var(--space-2);
  }
  .flag {
    font-family: var(--mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase;
    border-radius: var(--radius-md); padding: 0 var(--space-2);
  }
  .flag.pau { color: var(--warn); background: color-mix(in oklab, var(--warn) 14%, transparent); }

  .tags { display: flex; align-items: center; gap: var(--space-1); flex-wrap: wrap; }
  .chip {
    display: inline-flex; align-items: center; gap: 2px; font-size: 10px;
    color: var(--text); background: var(--surface3); border: 1px solid var(--border);
    border-radius: var(--radius-md); padding: 1px var(--space-1) 1px var(--space-2);
  }
  .chip .x {
    background: transparent; border: 0; color: var(--text3); cursor: pointer;
    font-size: 9px; padding: 0 2px; line-height: 1;
  }
  .chip .x:hover { color: var(--danger); }
  .tagin {
    background: transparent; border: 1px dashed var(--border); border-radius: var(--radius-md);
    color: var(--text2); font-size: 10px; padding: 1px var(--space-2); width: 72px;
    font-family: var(--mono);
  }
  .tagin:focus { outline: none; border-color: var(--atomizer); border-style: solid; }

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

  .goalwrap, .objwrap { display: flex; flex-direction: column; gap: var(--space-1); }
  .prompt.obj {
    background: color-mix(in oklab, var(--atomizer) 6%, var(--surface2));
    border-color: color-mix(in oklab, var(--atomizer) 30%, var(--border));
  }

  .lifecycle {
    display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap;
    padding-top: var(--space-1);
  }
  .lc {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: var(--radius-md); color: var(--text2); cursor: pointer;
    font-family: var(--mono); font-size: 10px; letter-spacing: 0.5px;
    text-transform: uppercase; padding: var(--space-1) var(--space-2);
  }
  .lc:hover:not(:disabled) { color: var(--text); border-color: var(--atomizer); }
  .lc:disabled { opacity: 0.4; cursor: default; }
  .lc.danger { color: var(--danger); border-color: color-mix(in oklab, var(--danger) 50%, var(--border)); }
  .lc.danger:hover:not(:disabled) { background: color-mix(in oklab, var(--danger) 14%, transparent); }
  .confirmcopy { font-size: 11px; color: var(--warn); }

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
