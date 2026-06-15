<script lang="ts">
  /**
   * DAG telemetry page — two panels:
   *   left  : a simple task list (orch_status), live-polled, state-badged;
   *   right : the selected task's live conversation (its placement transcript +
   *           a composer that splices human messages into the agent).
   *
   * Selecting a task on the left drives the right panel (onSelectTask). The list
   * is intentionally swappable: svc-orchestrator's @awm/dag-graph flowchart drops
   * in behind the same onSelect contract once it ships. Orchestrator EVENT
   * telemetry (the @awm/telemetry live stream) is owned by svc-orchestrator's
   * emission seam; until it lands we poll orch_status for live state.
   */
  import { onDestroy, untrack } from 'svelte';
  import TaskList from './lib/TaskList.svelte';
  import TaskChat from './lib/TaskChat.svelte';
  import { fetchStatus, type OrchStatus, type OrchTask } from './lib/orch';

  const POLL_MS = 3000;

  function initialProject(): string {
    if (typeof window === 'undefined') return '';
    const p = new URLSearchParams(window.location.search).get('project');
    return p ?? localStorage.getItem('awm.dag.project') ?? '';
  }

  let project = $state(untrack(initialProject));
  let status = $state<OrchStatus | null>(null);
  let error = $state<string | null>(null);
  let selectedId = $state<string | null>(null);

  const tasks = $derived(status?.tasks ?? []);
  const selected = $derived(
    tasks.find((t) => t.task_id === selectedId) ?? null,
  );

  async function refresh() {
    try {
      status = await fetchStatus(project.trim() || undefined);
      error = null;
    } catch (err) {
      error = (err as Error).message;
    }
  }

  function onSelect(t: OrchTask) {
    selectedId = t.task_id;
  }

  function applyProject() {
    if (typeof window !== 'undefined') {
      localStorage.setItem('awm.dag.project', project.trim());
    }
    void refresh();
  }

  // Poll loop.
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
    <form
      class="proj"
      onsubmit={(e) => {
        e.preventDefault();
        applyProject();
      }}
    >
      <input class="field mono" name="project" placeholder="project (blank = all)" bind:value={project} aria-label="project" />
      <button class="btn mono" type="submit">load</button>
    </form>
    {#if status}
      <span class="counts mono">
        {#each Object.entries(status.counts) as [state, n]}
          <span class="count" data-state={state}>{state}:{n}</span>
        {/each}
        {#if status.complete}<span class="count done">complete</span>{/if}
      </span>
    {/if}
  </header>

  {#if error}<p class="error mono">{error}</p>{/if}

  <div class="panels">
    <section class="left">
      <TaskList {tasks} {selectedId} {onSelect} />
    </section>
    <section class="right">
      {#if selected && selected.workspace_slug}
        {#key `${project}/${selected.workspace_slug}`}
          <TaskChat
            project={status?.project ?? project.trim()}
            workspaceSlug={selected.workspace_slug}
            goal={selected.goal}
          />
        {/key}
      {:else if selected}
        <p class="hint mono">task <code>{selected.task_id}</code> ({selected.state}) has no workspace unit yet — nothing to attach to.</p>
      {:else}
        <p class="hint mono">select a task to watch its live conversation.</p>
      {/if}
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
  .proj { display: flex; gap: 6px; align-items: center; }
  .field {
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text, #ddd);
    padding: 4px 8px;
    font-size: 11px;
  }
  .field:focus { outline: none; border-color: var(--atomizer, #ffb74d); }
  .btn {
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text2, #bbb);
    padding: 4px 10px;
    font-size: 11px;
    text-transform: uppercase;
    cursor: pointer;
  }
  .btn:hover { border-color: var(--atomizer, #ffb74d); color: var(--text, #ddd); }
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
