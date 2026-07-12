<script lang="ts">
  import { onMount } from 'svelte';
  import { PanelLabel, Tag } from '@awm/primitives';
  import Skeleton from '../lib/Skeleton.svelte';
  import { listProjectAttention, listTasks } from '../lib/tasks/client';
  import type { ProjectAttention, Task } from '../lib/tasks/types';
  import { TASK_LANES } from '../lib/tasks/types';
  import { LANE_VAR, REVIEW_VAR } from '../lib/laneStyle';

  let { onopenproject = (_name: string) => {} }: { onopenproject?: (name: string) => void } =
    $props();

  let projects = $state<ProjectAttention[]>([]);
  let needsInput = $state<Task[]>([]);

  onMount(async () => {
    projects = await listProjectAttention();
    // Flatten all mock tasks flagged needs-human across projects.
    const all = await Promise.all(projects.map((p) => listTasks(p.name)));
    needsInput = all.flat().filter((t) => t.needsHuman);
  });

  function laneSegments(p: ProjectAttention) {
    const b = p.taskBreakdown;
    if (!b) return [];
    return TASK_LANES.map((s) => ({ status: s, n: b[s] })).filter((x) => x.n > 0);
  }
</script>

<div class="ov">
  <!-- system status — real metrics will replace these (status-table) -->
  <section>
    <PanelLabel tone="dim">system status</PanelLabel>
    <div class="cards">
      <Skeleton variant="card" label="hub health" />
      <Skeleton variant="card" label="services" />
      <Skeleton variant="card" label="active scopes" />
    </div>
  </section>

  <!-- projects needing attention — mock-scored, sorted desc -->
  <section>
    <PanelLabel tone="dim">projects needing attention</PanelLabel>
    <ul class="rows">
      {#each projects as p (p.name)}
        <li>
          <button class="prow" onclick={() => onopenproject(p.name)}>
            <span class="pname">{p.name}</span>
            <span class="bar">
              {#each laneSegments(p) as seg (seg.status)}
                <span
                  class="seg"
                  style:background={LANE_VAR[seg.status]}
                  style:flex={seg.n}
                  title={`${seg.status}: ${seg.n}`}
                ></span>
              {/each}
            </span>
            <span class="score" class:hot={p.attentionScore >= 60}>{p.attentionScore}</span>
          </button>
        </li>
      {/each}
    </ul>
  </section>

  <!-- needs your input -->
  <section>
    <PanelLabel tone="dim">needs your input</PanelLabel>
    <ul class="rows">
      {#each needsInput as t (t.id)}
        <li>
          <div class="irow" style:--accent={t.status === 'error' ? LANE_VAR.error : REVIEW_VAR}>
            <span class="idot"></span>
            <div class="itext">
              <span class="ititle">{t.title}</span>
              <span class="imeta">{t.projectId}{t.assignedScope ? ` · ${t.assignedScope}` : ''}</span>
            </div>
            <Tag tone={t.status === 'error' ? 'danger' : 'mgr'}>
              {t.status === 'error' ? 'blocked' : 'review'}
            </Tag>
          </div>
        </li>
      {:else}
        <Skeleton variant="line" lines={2} />
      {/each}
    </ul>
  </section>
</div>

<style>
  .ov {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
    padding: var(--space-5);
  }
  section {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .cards {
    display: grid;
    grid-template-columns: 1fr;
    gap: var(--space-3);
  }
  .rows {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .prow {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    width: 100%;
    text-align: left;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: var(--space-3);
    color: var(--text);
    cursor: pointer;
    font: inherit;
  }
  .prow:hover { border-color: var(--border2); }
  .pname { font-family: var(--mono); font-size: 12px; flex: none; min-width: 84px; }
  .bar {
    flex: 1;
    display: flex;
    height: 8px;
    border-radius: var(--radius-sm);
    overflow: hidden;
    background: var(--surface3);
  }
  .seg { display: block; }
  .score {
    flex: none;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text2);
    min-width: 22px;
    text-align: right;
  }
  .score.hot { color: var(--danger); font-weight: 600; }

  .irow {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent, var(--border2));
    border-radius: var(--radius-md);
    padding: var(--space-3);
  }
  .idot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--accent, var(--text3)); flex: none;
  }
  .itext { display: flex; flex-direction: column; gap: 1px; flex: 1; min-width: 0; }
  .ititle { font-size: 12px; }
  .imeta { font-family: var(--mono); font-size: 10px; color: var(--text3); }

  /* desktop: status cards spread across a row */
  @media (min-width: 720px) {
    .cards { grid-template-columns: repeat(3, 1fr); }
  }
</style>
