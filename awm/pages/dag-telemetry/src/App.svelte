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
   * shared by both dag-graph primitives. The TaskList and FocusPanel share the
   * controlled-selection contract, so a neighbour click in Info re-selects the
   * list.
   */
  import { onDestroy } from 'svelte';
  import { TaskList, FocusPanel, SteeringChip, buildIndex } from '@awm/dag-graph';
  import {
    fetchDag,
    openNode,
    setTitle,
    setTags,
    setPaused,
    attachTask,
    setUserDone,
    fetchTaskDetail,
    cancelTask,
    retryTask,
    type DagSnapshot,
    type DagTask,
    type TaskDetail,
  } from '@awm/client';
  import { AgentChat } from '@awm/agent-chat';

  const POLL_MS = 3000;

  // Terminal states: a task here is finished — its retained transcript is shown
  // read-only (composer hidden) rather than as a live conversation.
  const TERMINAL = new Set(['completed', 'failed', 'abandoned']);
  let snapshot = $state<DagSnapshot | null>(null);
  let error = $state<string | null>(null);
  let selectedId = $state<string | null>(null);
  let activeTab = $state<'info' | 'chat'>('info');
  let query = $state('');

  // Optimistic edit overlay: a user edit to title/tags/paused is applied locally
  // at once and held until the 3s poll confirms the server agrees (otherwise the
  // poll would visibly snap the field back). Keyed by task_id; each field cleared
  // independently once the snapshot matches it ("my edit wins" until confirmed).
  type Edit = { title?: string; tags?: string[]; paused?: boolean };
  let edits = $state<Record<string, Edit>>({});

  // Steering + lifecycle are SERVER-AUTHORITATIVE (they change from both sides),
  // so they never enter the optimistic `edits` overlay — the controls just go
  // busy-until-poll and the chip/flags flip when the next snapshot lands.
  let now = $state(Date.now()); // bumped each poll so attention ages refresh.
  let detail = $state<TaskDetail | null>(null); // on-selection heavier read.
  let lifecycleBusy = $state(false);
  let steerBusy = $state(false);

  function sameTags(a: string[], b: string[]): boolean {
    return a.length === b.length && a.every((x, i) => x === b[i]);
  }
  function reconcileEdits(snap: DagSnapshot) {
    let changed = false;
    for (const t of snap.tasks) {
      const e = edits[t.task_id];
      if (!e) continue;
      if (e.title !== undefined && e.title === (t.title ?? '')) { delete e.title; changed = true; }
      if (e.paused !== undefined && e.paused === !!t.paused) { delete e.paused; changed = true; }
      if (e.tags !== undefined && sameTags(e.tags, t.tags ?? [])) { delete e.tags; changed = true; }
      if (Object.keys(e).length === 0) delete edits[t.task_id];
    }
    if (changed) edits = { ...edits };
  }

  // One index, shared by TaskList + FocusPanel; built from the OVERLAID snapshot
  // so both views reflect optimistic edits, then re-derives on each fresh poll.
  const tasks = $derived<DagTask[]>(
    (snapshot?.tasks ?? []).map((t) => {
      const e = edits[t.task_id];
      return e ? { ...t, ...e } : t;
    }),
  );
  const overlaid = $derived<DagSnapshot | null>(
    snapshot ? { ...snapshot, tasks } : null,
  );
  const index = $derived(overlaid ? buildIndex(overlaid) : null);
  const selected = $derived<DagTask | null>(
    tasks.find((t) => t.task_id === selectedId) ?? null,
  );
  const isTerminal = $derived(!!selected && TERMINAL.has(selected.state));

  // The orchestrator plan is a single GLOBAL DAG — always fetch the whole graph;
  // there is no per-project view filter.
  async function refresh() {
    try {
      const snap = await fetchDag();
      reconcileEdits(snap);
      snapshot = snap;
      now = Date.now();
      error = null;
    } catch (err) {
      error = (err as Error).message;
    }
    // Refresh the selected task's objective/detail alongside the poll so the
    // objective record appears when the detach handshake commits it.
    void refreshDetail(selectedId);
  }

  // On-selection (and per-poll) heavier read: the objective text, plan ref, and
  // attempt memories. Guarded against a selection race — only apply if the id is
  // still selected when the response lands.
  async function refreshDetail(id: string | null) {
    if (!id) { detail = null; return; }
    try {
      const d = await fetchTaskDetail(id);
      if (id === selectedId) detail = d.ok ? d : null;
    } catch {
      /* leave the last-good detail in place */
    }
  }

  // Fetch detail immediately on a selection change (don't wait for the next poll).
  let lastDetailId: string | null = null;
  $effect(() => {
    const id = selectedId;
    if (id !== lastDetailId) {
      lastDetailId = id;
      detail = null;
      void refreshDetail(id);
    }
  });

  // --- Create a new task ---------------------------------------------------
  // One click: place an attended worker on an EMPTY goal (born paused, idle until
  // the first human turn), select it, and drop straight into the chat composer.

  let busyNew = $state(false);

  async function newTask() {
    if (busyNew) return;
    busyNew = true;
    error = null;
    try {
      const res = await openNode('');
      await refresh();
      selectedId = res.task_id;
      activeTab = 'chat';
    } catch (err) {
      error = `create task failed: ${(err as Error).message}`;
    } finally {
      busyNew = false;
    }
  }

  // --- Edit handlers (optimistic, then persist) ----------------------------

  function applyEdit(taskId: string, patch: Edit) {
    edits[taskId] = { ...edits[taskId], ...patch };
    edits = { ...edits };
  }
  async function onSetTitle(taskId: string, title: string) {
    applyEdit(taskId, { title });
    try { await setTitle(taskId, title); } catch (err) { error = (err as Error).message; }
  }
  async function onSetTags(taskId: string, tags: string[]) {
    applyEdit(taskId, { tags });
    try { await setTags(taskId, tags); } catch (err) { error = (err as Error).message; }
  }
  async function onSetPaused(slug: string, paused: boolean, taskId: string) {
    applyEdit(taskId, { paused });
    try { await setPaused(slug, paused, taskId); } catch (err) { error = (err as Error).message; }
  }

  // --- Lifecycle controls (busy-until-poll, NO optimistic overlay) ---------

  type LifecycleFn = (taskId: string) => Promise<{ ok?: boolean; error?: string }>;
  async function runLifecycle(fn: LifecycleFn, taskId: string) {
    if (lifecycleBusy) return;
    lifecycleBusy = true;
    error = null;
    try {
      const res = await fn(taskId);
      if (res && res.ok === false) error = res.error ?? 'action failed';
      await refresh();
    } catch (err) {
      error = (err as Error).message;
    } finally {
      lifecycleBusy = false;
    }
  }
  const onCancel = (id: string) => runLifecycle(cancelTask, id);
  const onRetry = (id: string) => runLifecycle(retryTask, id);

  // --- Steering controls (attach + hand-off; server-authoritative) ---------

  async function doAttach() {
    if (!selected?.workspace_slug || steerBusy) return;
    steerBusy = true;
    error = null;
    try {
      await attachTask(selected.workspace_slug, selected.task_id);
      await refresh();
    } catch (err) {
      error = (err as Error).message;
    } finally {
      steerBusy = false;
    }
  }

  async function doSetUserDone(done: boolean) {
    if (!selected?.workspace_slug || steerBusy) return;
    steerBusy = true;
    error = null;
    try {
      const r = await setUserDone(selected.workspace_slug, done, selected.task_id);
      if (r && r.ok === false) error = r.error ?? 'not attached';
      await refresh();
    } catch (err) {
      error = (err as Error).message;
    } finally {
      steerBusy = false;
    }
  }

  // Attach-on-send seam handed to AgentChat: the first message while detached is
  // the explicit interrupt. Awaited on the single send path (no extra post),
  // then a refresh so the attach flag flips in the chip.
  async function attachForSend() {
    if (!selected?.workspace_slug) return;
    try {
      await attachTask(selected.workspace_slug, selected.task_id);
    } finally {
      void refresh();
    }
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
    <input
      class="search mono"
      type="search"
      placeholder="search title · tags · goal"
      aria-label="search tasks"
      bind:value={query}
    />
    <button
      class="newbtn mono"
      type="button"
      title="start a new agent (you type the first message)"
      disabled={busyNew}
      onclick={newTask}
    >{busyNew ? 'starting…' : '+ new task'}</button>
  </header>

  {#if error}<p class="error mono">{error}</p>{/if}

  <div class="panels">
    <section class="left">
      {#if index}
        <TaskList {index} {query} nowMs={now} selectedTaskId={selectedId} onSelectTask={(id) => (selectedId = id)} />
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
              <FocusPanel
                {index}
                selectedTaskId={selectedId}
                onSelectTask={(id) => (selectedId = id)}
                {onSetTitle}
                {onSetTags}
                {onSetPaused}
                {detail}
                {onCancel}
                {onRetry}
                {lifecycleBusy}
              />
            </div>
          {:else}
            <p class="hint mono">loading the plan…</p>
          {/if}
        {:else if selected && selected.workspace_slug}
          <!-- Steering bar: read-only chip + explicit attach / hand-off controls.
               Sending in the chat below also attaches (attach-on-send). -->
          {#if !isTerminal}
            <div class="steerbar">
              <SteeringChip task={selected} />
              <div class="steeractions">
                {#if !selected.attached}
                  <button
                    class="steerbtn mono"
                    type="button"
                    title="attach to steer without sending a message"
                    disabled={steerBusy}
                    onclick={doAttach}
                  >{steerBusy ? '…' : 'attach'}</button>
                {:else if !selected.steer_user_done}
                  <button
                    class="steerbtn mono"
                    type="button"
                    title="signal you're done — the node detaches automatically once the agent is ready"
                    disabled={steerBusy}
                    onclick={() => doSetUserDone(true)}
                  >{steerBusy ? '…' : 'hand off'}</button>
                {:else}
                  <span class="handoff-pending mono" title="waiting for the agent to confirm it understands">you: done — waiting on agent</span>
                  <button
                    class="steerbtn mono"
                    type="button"
                    title="un-say done (you're still attached)"
                    disabled={steerBusy}
                    onclick={() => doSetUserDone(false)}
                  >{steerBusy ? '…' : 'un-say done'}</button>
                {/if}
              </div>
            </div>
          {/if}
          {#key selected.workspace_slug}
            <AgentChat
              slug={selected.workspace_slug}
              embedded
              readonly={isTerminal}
              attached={selected.attached}
              onAttach={attachForSend}
            />
          {/key}
        {:else if selected}
          <p class="hint mono">task <code>{selected.task_id}</code> is <code>{selected.state}</code> — no agent attached yet (the planner mints its unit on dispatch).</p>
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
  .newbtn {
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text2, #bbb);
    padding: 4px 10px;
    font-size: 11px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    cursor: pointer;
    flex: 0 0 auto;
  }
  .newbtn:hover:not(:disabled) {
    background: var(--surface3, #2a2a2a);
    color: var(--text, #ddd);
    border-color: var(--atomizer, #ffb74d);
  }
  .newbtn:disabled { opacity: 0.5; cursor: default; }
  .search {
    flex: 1 1 200px;
    min-width: 0;
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text, #ddd);
    padding: 4px 8px;
    font-size: 11px;
  }
  .search:focus { outline: none; border-color: var(--atomizer, #ffb74d); }
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
  .steerbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    padding: 8px 14px;
    border-bottom: 1px solid var(--border, #333);
    background: var(--surface, #1a1a1a);
    flex: 0 0 auto;
  }
  .steeractions { display: flex; align-items: center; gap: 8px; }
  .steerbtn {
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text2, #bbb);
    padding: 4px 10px;
    font-size: 11px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    cursor: pointer;
  }
  .steerbtn:hover:not(:disabled) {
    background: var(--surface3, #2a2a2a);
    color: var(--text, #ddd);
    border-color: var(--atomizer, #ffb74d);
  }
  .steerbtn:disabled { opacity: 0.5; cursor: default; }
  .handoff-pending {
    font-size: 10px;
    color: var(--atomizer, #ffb74d);
    letter-spacing: 0.5px;
    text-transform: uppercase;
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
