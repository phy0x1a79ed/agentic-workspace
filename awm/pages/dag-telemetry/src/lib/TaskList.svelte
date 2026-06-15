<script lang="ts">
  /**
   * Left panel — a simple task list (for now). Renders the orch_status rows as
   * a flat, state-badged list; clicking a task selects it (emits onSelect) and
   * the right panel attaches to that task's transcript. Built so svc-orchestrator's
   * @awm/dag-graph flowchart can drop in behind the same onSelect contract.
   */
  import type { OrchTask } from './orch';

  interface Props {
    tasks: OrchTask[];
    selectedId: string | null;
    onSelect: (task: OrchTask) => void;
  }
  let { tasks, selectedId, onSelect }: Props = $props();
</script>

<ul class="task-list">
  {#each tasks as t (t.task_id)}
    <li>
      <button
        class="task"
        class:selected={t.task_id === selectedId}
        onclick={() => onSelect(t)}
        title={t.task_id}
      >
        <span class="badge mono" data-state={t.state}>{t.state}</span>
        <span class="goal">{t.goal || t.task_id}</span>
        {#if t.is_root}<span class="root mono">root</span>{/if}
        {#if t.attached}<span class="attached mono" title="a human is attached">●</span>{/if}
      </button>
    </li>
  {:else}
    <li class="empty mono">no tasks</li>
  {/each}
</ul>

<style>
  .task-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .task {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    text-align: left;
    background: var(--surface, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    color: var(--text, #ddd);
    padding: 7px 9px;
    cursor: pointer;
    font-size: 12px;
  }
  .task:hover {
    border-color: var(--atomizer, #ffb74d);
    background: var(--surface3, #2a2a2a);
  }
  .task.selected {
    border-color: var(--atomizer, #ffb74d);
    background: color-mix(in oklab, var(--atomizer, #ffb74d) 12%, transparent);
  }
  .badge {
    flex: 0 0 auto;
    font-size: 9px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    padding: 2px 5px;
    border-radius: 3px;
    background: var(--surface2, #222);
    color: var(--text2, #bbb);
  }
  /* A few state accents; the rest fall back to the neutral badge. */
  .badge[data-state='active'] { color: #7fd17f; }
  .badge[data-state='completed'] { color: #5aa7ff; }
  .badge[data-state='failed'],
  .badge[data-state='abandoned'] { color: var(--warn, #f55); }
  .badge[data-state='planning'],
  .badge[data-state='verifying_plan'],
  .badge[data-state='decomposing'] { color: var(--atomizer, #ffb74d); }
  .goal {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .root {
    flex: 0 0 auto;
    font-size: 9px;
    color: var(--text3, #888);
    text-transform: uppercase;
  }
  .attached {
    flex: 0 0 auto;
    color: var(--atomizer, #ffb74d);
    font-size: 10px;
  }
  .empty {
    color: var(--text3, #888);
    font-size: 12px;
    padding: 8px 9px;
  }
  .mono { font-family: var(--mono, monospace); }
</style>
