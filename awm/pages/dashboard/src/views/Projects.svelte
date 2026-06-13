<script lang="ts">
  import { onMount } from 'svelte';
  import { Button, Pill, PanelLabel, Tag } from '@awm/primitives';
  import Skeleton from '../lib/Skeleton.svelte';
  import { listProjectAttention, listTasks } from '../lib/tasks/client';
  import type { ProjectAttention, Task, TaskStatus } from '../lib/tasks/types';
  import { TASK_LANES } from '../lib/tasks/types';
  import { LANE_LABEL, LANE_TONE, LANE_VAR, REVIEW_VAR } from '../lib/laneStyle';

  // App sets this when the operator taps a project from Overview.
  let { requestedProject = '' }: { requestedProject?: string } = $props();

  type Mode = 'list' | 'board' | 'task';
  let mode = $state<Mode>('list');
  let project = $state('');
  let projects = $state<ProjectAttention[]>([]);
  let tasks = $state<Task[]>([]);
  let activeTaskId = $state<string | null>(null);
  let filter = $state<TaskStatus | 'all'>('all');

  let activeTask = $derived(tasks.find((t) => t.id === activeTaskId) ?? null);

  onMount(async () => {
    projects = await listProjectAttention();
    if (requestedProject) openProject(requestedProject);
  });

  // React to a project requested from another view.
  $effect(() => {
    if (requestedProject && requestedProject !== project) openProject(requestedProject);
  });

  async function openProject(name: string) {
    project = name;
    tasks = await listTasks(name);
    filter = 'all';
    mode = 'board';
  }

  function lane(status: TaskStatus): Task[] {
    return tasks.filter((t) => t.status === status);
  }
  function depTitle(id: string): string {
    return tasks.find((t) => t.id === id)?.title ?? id;
  }
</script>

{#if mode === 'list'}
  <div class="pad">
    <PanelLabel tone="dim">projects · sorted by attention</PanelLabel>
    <ul class="plist">
      {#each projects as p (p.name)}
        <li>
          <button class="prow" onclick={() => openProject(p.name)}>
            <span class="pname">{p.name}</span>
            <span class="bar">
              {#each TASK_LANES as s (s)}
                {#if p.taskBreakdown && p.taskBreakdown[s] > 0}
                  <span class="seg" style:background={LANE_VAR[s]} style:flex={p.taskBreakdown[s]}></span>
                {/if}
              {/each}
            </span>
            <span class="score" class:hot={p.attentionScore >= 60}>{p.attentionScore}</span>
          </button>
        </li>
      {/each}
    </ul>
  </div>

{:else if mode === 'board'}
  <div class="board-wrap">
    <header class="subhead">
      <button class="back" onclick={() => (mode = 'list')}>‹ projects</button>
      <span class="crumb">{project}</span>
    </header>

    <!-- mobile: filter chips + single list -->
    <div class="mobile-board">
      <div class="chips">
        <Pill tone={filter === 'all' ? 'atomizer' : 'neutral'} aria-pressed={filter === 'all'} onclick={() => (filter = 'all')}>all</Pill>
        {#each TASK_LANES as s (s)}
          <Pill tone={filter === s ? 'atomizer' : 'neutral'} aria-pressed={filter === s} onclick={() => (filter = s)}>
            {LANE_LABEL[s]} {lane(s).length}
          </Pill>
        {/each}
      </div>
      <ul class="cards-col">
        {#each tasks.filter((t) => filter === 'all' || t.status === filter) as t (t.id)}
          {@render taskCard(t)}
        {/each}
      </ul>
    </div>

    <!-- desktop: five lanes side by side -->
    <div class="desktop-board">
      {#each TASK_LANES as s (s)}
        <div class="lane">
          <div class="lane-head">
            <span class="lane-dot" style:background={LANE_VAR[s]}></span>
            <PanelLabel tone="dim">{LANE_LABEL[s]}</PanelLabel>
            <span class="lane-n">{lane(s).length}</span>
          </div>
          <ul class="cards-col">
            {#each lane(s) as t (t.id)}
              {@render taskCard(t)}
            {/each}
          </ul>
        </div>
      {/each}
    </div>
  </div>

{:else if mode === 'task' && activeTask}
  {@const t = activeTask}
  <div class="pad detail">
    <header class="subhead">
      <button class="back" onclick={() => (mode = 'board')}>‹ {project}</button>
      <Tag tone={LANE_TONE[t.status]}>{t.status}</Tag>
    </header>
    <h2>{t.title}</h2>
    <p class="scope">{t.assignedScope ?? 'unassigned'}</p>

    <PanelLabel tone="dim">acceptance criteria</PanelLabel>
    {#if t.acceptanceCriteria.length}
      <ul class="crit">
        {#each t.acceptanceCriteria as c (c)}<li>{c}</li>{/each}
      </ul>
    {:else}
      <p class="muted">none recorded</p>
    {/if}

    {#if t.result}
      <PanelLabel tone="dim">result</PanelLabel>
      <p class="result" class:bad={!t.result.success}>{t.result.summary}</p>
    {/if}

    <PanelLabel tone="dim">agent transcript</PanelLabel>
    <Skeleton label="tts-history (agent room transcript)" height="120px" />

    <div class="controls">
      <Button kind="primary" size="sm">Retry</Button>
      <Button kind="ghost" size="sm">Intervene</Button>
      <Button kind="ghost" size="sm">Resolve</Button>
    </div>
  </div>
{/if}

{#snippet taskCard(t: Task)}
  <li>
    <button
      class="tcard"
      style:--accent={t.needsHuman && t.status === 'completed' ? REVIEW_VAR : LANE_VAR[t.status]}
      onclick={() => { activeTaskId = t.id; mode = 'task'; }}
    >
      <div class="trow">
        <span class="ttitle">{t.title}</span>
        {#if t.needsHuman && t.status === 'completed'}
          <Tag tone="mgr">review</Tag>
        {:else}
          <Tag tone={LANE_TONE[t.status]}>{t.status}</Tag>
        {/if}
      </div>
      <div class="tmeta">
        {#if t.assignedScope}<span class="chip">{t.assignedScope}</span>{/if}
        {#if t.status === 'blocked' && t.dependsOn.length}
          <span class="chip dep">depends on · {depTitle(t.dependsOn[0])}</span>
        {/if}
      </div>
    </button>
  </li>
{/snippet}

<style>
  .pad { padding: var(--space-5); display: flex; flex-direction: column; gap: var(--space-3); }

  /* project list */
  .plist { list-style: none; display: flex; flex-direction: column; gap: var(--space-2); }
  .prow {
    display: flex; align-items: center; gap: var(--space-3); width: 100%;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius-md); padding: var(--space-3); color: var(--text);
    cursor: pointer; font: inherit; text-align: left;
  }
  .prow:hover { border-color: var(--border2); }
  .pname { font-family: var(--mono); font-size: 12px; min-width: 84px; }
  .bar { flex: 1; display: flex; height: 8px; border-radius: var(--radius-sm); overflow: hidden; background: var(--surface3); }
  .seg { display: block; }
  .score { font-family: var(--mono); font-size: 11px; color: var(--text2); min-width: 22px; text-align: right; }
  .score.hot { color: var(--danger); font-weight: 600; }

  /* board chrome */
  .board-wrap { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-4) var(--space-4) var(--space-6); }
  .subhead { display: flex; align-items: center; gap: var(--space-3); }
  .back {
    background: none; border: none; color: var(--atomizer); font: inherit;
    font-family: var(--mono); font-size: 12px; cursor: pointer; padding: 0;
  }
  .crumb { font-family: var(--mono); font-size: 13px; }

  .chips { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-bottom: var(--space-2); }
  .cards-col { list-style: none; display: flex; flex-direction: column; gap: var(--space-2); }

  .tcard {
    display: flex; flex-direction: column; gap: var(--space-2); width: 100%;
    text-align: left; background: var(--surface);
    border: 1px solid var(--border); border-left: 3px solid var(--accent);
    border-radius: var(--radius-md); padding: var(--space-3);
    color: var(--text); cursor: pointer; font: inherit;
  }
  .tcard:hover { border-color: var(--border2); border-left-color: var(--accent); }
  .trow { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); }
  .ttitle { font-size: 12px; line-height: 1.3; }
  .tmeta { display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .chip {
    font-family: var(--mono); font-size: 9px; color: var(--text3);
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: 1px var(--space-1);
  }
  .chip.dep { color: var(--warn); border-color: color-mix(in oklab, var(--warn) 40%, var(--border)); }

  /* responsive board: mobile chips-list vs desktop lanes */
  .desktop-board { display: none; }
  @media (min-width: 720px) {
    .mobile-board { display: none; }
    .desktop-board {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: var(--space-3);
      align-items: start;
    }
    .lane { display: flex; flex-direction: column; gap: var(--space-2); }
    .lane-head { display: flex; align-items: center; gap: var(--space-2); }
    .lane-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
    .lane-n { margin-left: auto; font-family: var(--mono); font-size: 11px; color: var(--text3); }
  }

  /* task detail */
  .detail h2 { font-size: 15px; font-weight: 600; }
  .scope { font-family: var(--mono); font-size: 11px; color: var(--text2); margin-top: -4px; }
  .crit { padding-left: var(--space-5); display: flex; flex-direction: column; gap: var(--space-1); font-size: 12px; }
  .muted { color: var(--text3); font-size: 12px; }
  .result { font-size: 12px; color: var(--text2); }
  .result.bad { color: var(--danger); }
  .controls { display: flex; gap: var(--space-2); margin-top: var(--space-3); }
</style>
