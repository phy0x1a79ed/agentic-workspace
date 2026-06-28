<script lang="ts">
  /**
   * DAG telemetry page — the orchestrator's whole plan, in one view:
   *   left  : @awm/dag-graph TaskList — every task grouped by state, runnable /
   *           in-flight first, done collapsed. Row click selects a task.
   *   right : two tabs for the selected task —
   *             · Info — @awm/dag-graph FocusPanel: the task's upstream deps and
   *               downstream dependents, each labelled by the connecting contract;
   *               clicking a neighbour walks the DAG one hop at a time.
   *             · Chat — its live placement transcript + a composer that splices
   *               human messages into the agent.
   *
   * One `orch_dag` poll feeds everything: a single DagSnapshot → one buildIndex,
   * shared by both dag-graph primitives, plus the header counts (derived, no 2nd
   * op). The TaskList and FocusPanel share the controlled-selection contract, so
   * a neighbour click in Info re-selects the list.
   */
  import { onDestroy } from 'svelte';
  import { TaskList, FocusPanel, buildIndex } from '@awm/dag-graph';
  import { fetchDag, type DagSnapshot, type DagTask } from '@awm/client';
  import TaskChat from './lib/TaskChat.svelte';

  const POLL_MS = 3000;

  // The orchestrator owns a single global DAG — there is nothing to filter.
  let snapshot = $state<DagSnapshot | null>(null);
  let error = $state<string | null>(null);
  let selectedId = $state<string | null>(null);
  let activeTab = $state<'info' | 'chat'>('info');

  // One index, shared by TaskList + FocusPanel; re-derives on each fresh snapshot.
  const index = $derived(snapshot ? buildIndex(snapshot) : null);
  const tasks = $derived<DagTask[]>(snapshot?.tasks ?? []);
  const selected = $derived<DagTask | null>(
    tasks.find((t) => t.task_id === selectedId) ?? null,
  );

  // Header summary — per-state counts + overall completion, off the snapshot.
  const counts = $derived.by<[string, number][]>(() => {
    const m = new Map<string, number>();
    for (const t of tasks) m.set(t.state, (m.get(t.state) ?? 0) + 1);
    return [...m.entries()];
  });
  const complete = $derived(
    !!snapshot?.root_id &&
      tasks.find((t) => t.task_id === snapshot!.root_id)?.state === 'completed',
  );

  async function refresh() {
    try {
      const next = await fetchDag();
      snapshot = next;
      error = null;
    } catch (err) {
      error = (err as Error).message;
    }
  }

  // Poll loop — the single global DAG, refreshed on an interval.
  let timer: ReturnType<typeof setInterval> | null = null;
  $effect(() => {
    void refresh();
    timer = setInterval(() => void refresh(), POLL_MS);
    return () => {
      if (timer) clearInterval(timer);
    };
  });
  onDestroy(() => {
    if (timer) clearInterval(timer);
  });
</script>

<main class="dag">
  <header class="top">
    <h1 class="mono">DAG telemetry</h1>
    {#if snapshot}
      <span class="counts mono">
        {#each counts as [state, n]}
          <span class="count" data-state={state}>{state}:{n}</span>
        {/each}
        {#if complete}<span class="count done">complete</span>{/if}
      </span>
    {/if}
  </header>

  {#if error}<p class="error mono">{error}</p>{/if}

  <div class="panels">
    <section class="left">
      {#if index}
        <TaskList {index} selectedTaskId={selectedId} onSelectTask={(id) => (selectedId = id)} />
      {:else}
        <p class="hint mono">loading the plan…</p>
      {/if}
    </section>

    <section class="right">
      <nav class="tabs mono">
        <button class="tab" class:active={activeTab === 'info'} type="button" onclick={() => (activeTab = 'info')}>info</button>
        <button class="tab" class:active={activeTab === 'chat'} type="button" onclick={() => (activeTab = 'chat')}>chat</button>
      </nav>

      <div class="tabbody">
        {#if activeTab === 'info'}
          {#if index}
            <div class="focuswrap">
              <FocusPanel {index} selectedTaskId={selectedId} onSelectTask={(id) => (selectedId = id)} />
            </div>
          {:else}
            <p class="hint mono">loading the plan…</p>
          {/if}
        {:else if selected && selected.workspace_slug}
          {#key selected.workspace_slug}
            <TaskChat
              workspaceSlug={selected.workspace_slug}
              goal={selected.goal}
            />
          {/key}
        {:else if selected}
          <p class="hint mono">task <code>{selected.task_id}</code> ({selected.state}) has no workspace unit yet — nothing to attach to.</p>
        {:else}
          <p class="hint mono">select a task to watch its live conversation.</p>
        {/if}
      </div>
    </section>
  </div>
</main>

<style>
  .dag {
    display: flex;
    flex-direction: column;
    height: 100vh;
    color: var(--text, #ddd);
    background: var(--bg, #111);
    font-family: system-ui, sans-serif;
  }
  .top {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border, #333);
    background: var(--surface, #1a1a1a);
    flex: 0 0 auto;
    flex-wrap: wrap;
  }
  h1 {
    margin: 0;
    font-size: 13px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .counts {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    font-size: 10px;
    color: var(--text3, #888);
  }
  .count[data-state='active'] { color: #7fd17f; }
  .count[data-state='completed'] { color: #5aa7ff; }
  .count[data-state='failed'],
  .count[data-state='abandoned'] { color: var(--warn, #f55); }
  .count.done { color: #5aa7ff; }
  .error {
    margin: 0;
    padding: 6px 14px;
    background: color-mix(in oklab, var(--warn, #f55) 14%, transparent);
    color: var(--warn, #f55);
    font-size: 12px;
    flex: 0 0 auto;
  }
  .panels {
    flex: 1 1 auto;
    display: grid;
    grid-template-columns: minmax(260px, 360px) 1fr;
    min-height: 0;
  }
  .left {
    border-right: 1px solid var(--border, #333);
    padding: 10px;
    overflow-y: auto;
    min-height: 0;
  }
  .right {
    display: flex;
    flex-direction: column;
    min-height: 0;
    min-width: 0;
  }
  .tabs {
    display: flex;
    gap: 2px;
    padding: 6px 8px 0;
    border-bottom: 1px solid var(--border, #333);
    background: var(--surface, #1a1a1a);
    flex: 0 0 auto;
  }
  .tab {
    background: transparent;
    border: 1px solid transparent;
    border-bottom: none;
    border-radius: 4px 4px 0 0;
    color: var(--text3, #888);
    padding: 5px 14px;
    font-size: 11px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    cursor: pointer;
  }
  .tab:hover { color: var(--text2, #bbb); }
  .tab.active {
    color: var(--text, #ddd);
    border-color: var(--border, #333);
    background: var(--surface2, #222);
    margin-bottom: -1px;
  }
  .tabbody {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
    min-width: 0;
  }
  .focuswrap {
    flex: 1 1 auto;
    overflow-y: auto;
    min-height: 0;
    padding: 10px;
  }
  .hint {
    margin: auto;
    color: var(--text3, #888);
    font-size: 12px;
    padding: 1rem;
    text-align: center;
  }
  code { font-family: var(--mono, monospace); color: var(--text2, #bbb); }
  .mono { font-family: var(--mono, monospace); }
</style>
