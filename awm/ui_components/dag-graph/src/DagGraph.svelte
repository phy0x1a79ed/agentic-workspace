<!--
  DagGraph — presentational orchestrator-DAG view. The page owns fetch +
  selection + live patching; this component just indexes the snapshot and
  composes the two views, forwarding selection out via `onSelectTask`.

  A full node-link canvas is deliberately avoided (heavy + space-hungry). The
  list gives the global sense of the plan; the focus panel gives the selected
  task's immediate dependency neighbourhood, navigable one hop at a time.

  Live updates: hand a new `snapshot` reference (the page patches task states /
  contract delivery by id off the telemetry stream) — the derived index
  recomputes and every appearance of a shared node stays in sync.
-->
<script lang="ts">
  import type { DagSnapshot } from './types';
  import { buildIndex } from './graph-index';
  import TaskList from './TaskList.svelte';
  import FocusPanel from './FocusPanel.svelte';

  interface Props {
    snapshot: DagSnapshot;
    selectedTaskId?: string | null;
    onSelectTask?: (taskId: string) => void;
  }
  let { snapshot, selectedTaskId = null, onSelectTask }: Props = $props();

  const index = $derived(buildIndex(snapshot));
</script>

<div class="dag-graph">
  <div class="pane list-pane">
    <TaskList {index} {selectedTaskId} {onSelectTask} />
  </div>
  <div class="pane focus-pane">
    <FocusPanel {index} {selectedTaskId} {onSelectTask} />
  </div>
</div>

<style>
  .dag-graph {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr);
    gap: var(--space-4);
    align-items: start;
  }
  .pane { min-width: 0; }
  .focus-pane {
    position: sticky; top: var(--space-3);
    border-left: 1px solid var(--border);
    padding-left: var(--space-4);
  }
  @media (max-width: 720px) {
    .dag-graph { grid-template-columns: 1fr; }
    .focus-pane { position: static; border-left: 0; border-top: 1px solid var(--border);
                  padding-left: 0; padding-top: var(--space-3); }
  }
</style>
