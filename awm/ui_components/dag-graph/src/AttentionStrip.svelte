<!--
  AttentionStrip — a slim horizontal bar surfacing ONLY what genuinely needs the
  human, derived from the same 3s poll as everything else (no second feed):
    · wants  — nodes waiting on you (wants-steering flag, or attached/frozen)
    · broken — failed / abandoned nodes
  Each pill click-selects its task (drives the same FocusPanel selection). When
  both groups are empty the strip renders nothing (it takes no vertical space).
-->
<script lang="ts">
  import { deriveAttention, type AttentionEntry } from './attention';
  import type { DagTask } from './types';

  interface Props {
    /** The current (overlaid) task list — same array the TaskList sees. */
    tasks: DagTask[];
    selectedTaskId?: string | null;
    onSelectTask?: (taskId: string) => void;
    /** Re-derive tick: the page bumps this each poll so ages refresh. */
    nowMs?: number;
  }
  let { tasks, selectedTaskId = null, onSelectTask, nowMs }: Props = $props();

  const attention = $derived(deriveAttention(tasks, nowMs ?? Date.now()));
  const empty = $derived(
    attention.wants.length === 0 && attention.broken.length === 0,
  );

  // Compact age: seconds → "Ns", minutes → "Nm", hours → "Nh", else "Nd".
  function ago(ms: number): string {
    const s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h`;
    return `${Math.floor(h / 24)}d`;
  }
</script>

{#if !empty}
  <div class="strip" role="region" aria-label="needs attention">
    {#if attention.wants.length}
      <div class="grp wants">
        <span class="glbl" title="waiting on you — wants steering or attached">wants you</span>
        {#each attention.wants as e (e.taskId)}
          {@render pill(e)}
        {/each}
      </div>
    {/if}
    {#if attention.broken.length}
      <div class="grp broken">
        <span class="glbl" title="failed or abandoned">broken</span>
        {#each attention.broken as e (e.taskId)}
          {@render pill(e)}
        {/each}
      </div>
    {/if}
  </div>
{/if}

{#snippet pill(e: AttentionEntry)}
  <button
    class="pill {e.group}"
    class:selected={e.taskId === selectedTaskId}
    type="button"
    title={`${e.label} · ${e.state} · ${ago(e.ageMs)} ago`}
    onclick={() => onSelectTask?.(e.taskId)}
  >
    <span class="dot"></span>
    <span class="plabel">{e.label}</span>
    <span class="age">{ago(e.ageMs)}</span>
  </button>
{/snippet}

<style>
  .strip {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    flex-wrap: wrap;
    min-width: 0;
  }
  .grp {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    flex-wrap: wrap;
    min-width: 0;
  }
  .glbl {
    font-family: var(--mono);
    font-size: 9px;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: var(--text3);
  }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    max-width: 220px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 1px var(--space-2);
    cursor: pointer;
    color: var(--text2);
    font-size: 11px;
    line-height: 1.6;
    transition: border-color 120ms var(--ease-mech), color 120ms var(--ease-mech);
  }
  .pill:hover { color: var(--text); border-color: var(--atomizer); }
  .pill.selected { border-color: var(--atomizer); color: var(--text); }
  .dot { flex: 0 0 auto; width: 6px; height: 6px; border-radius: 50%; }
  .pill.wants .dot { background: var(--warn); }
  .pill.broken .dot { background: var(--danger); }
  .plabel {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .age { flex: 0 0 auto; font-family: var(--mono); font-size: 9px; color: var(--text3); }
</style>
