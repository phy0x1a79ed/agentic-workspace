<script lang="ts">
  import type { RoomAgent } from '$lib/api/client';

  interface Props {
    agents: RoomAgent[];
    selected: string[];
    onchange?: (keys: string[]) => void;
  }
  let { agents, selected, onchange }: Props = $props();

  function toggle(key: string) {
    const set = new Set(selected);
    if (set.has(key)) set.delete(key); else set.add(key);
    onchange?.([...set]);
  }
</script>

<div class="chips">
  {#if agents.length === 0}
    <em class="dim">no agents in room</em>
  {:else}
    {#each agents as a}
      {@const key = `scope:${a.scope}`}
      {@const on = selected.includes(key)}
      <button class="chip" class:on type="button" onclick={() => toggle(key)}>
        <span class="dot" class:live={!!a.live}></span>
        <span>{a.scope}</span>
        <span class="kind">{a.kind}</span>
      </button>
    {/each}
  {/if}
</div>

<style>
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    border: 1px solid var(--border);
    background: var(--surface2);
    border-radius: 4px;
    cursor: pointer;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text2);
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  .chip:hover { background: var(--surface3); border-color: var(--border2); }
  .chip.on {
    background: color-mix(in oklab, var(--atomizer) 20%, var(--surface2));
    border-color: color-mix(in oklab, var(--atomizer) 60%, var(--border));
    color: var(--text);
  }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text3); }
  .dot.live { background: var(--ok); }
  .kind { font-size: 9px; color: var(--text3); letter-spacing: 1px; text-transform: uppercase; }
  .dim  { color: var(--text3); font-size: 11px; font-style: italic; }

  @media (max-width: 720px) {
    .chip { min-height: 44px; padding: 10px 12px; font-size: 12px; }
  }
</style>
