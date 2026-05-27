<script lang="ts">
  import type { Room } from '$lib/api/client';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';

  interface Props {
    rooms: Room[];
    activeId: string | null;
    title?: string;
    onpick?: (id: string) => void;
  }
  let { rooms, activeId, title = 'FOCUS', onpick }: Props = $props();

  function pick(id: string) {
    if (onpick) onpick(id);
    else goto(`${base}/focus/${encodeURIComponent(id)}`);
  }
</script>

<aside class="sidebar">
  <header class="sb-header">
    <span class="panel-label">{title}</span>
    <span class="count mono">{rooms.length}</span>
  </header>
  <div class="rooms">
    {#if rooms.length === 0}
      <div class="empty"><em class="dim mono">no joined rooms</em></div>
    {:else}
      {#each rooms as r}
        <button class="room" class:active={r.id === activeId} type="button" onclick={() => pick(r.id)}>
          <div class="row1">
            <span class="id mono">{r.id}</span>
            <span class="status mono">{r.status}</span>
          </div>
          {#if r.topic}
            <div class="topic">{r.topic}</div>
          {/if}
        </button>
      {/each}
    {/if}
  </div>
</aside>

<style>
  .sidebar {
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    height: 100%;
  }
  .sb-header {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-shrink: 0;
  }
  .count { color: var(--text3); font-size: 10px; }
  .rooms { flex: 1; overflow-y: auto; padding: 6px; display: flex; flex-direction: column; gap: 4px; }
  .empty { padding: 14px 10px; }
  .mono  { font-family: var(--mono); }
  .dim   { color: var(--text3); }

  .room {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 8px 10px;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: 3px;
    text-align: left;
    transition: background 0.12s, border-color 0.12s;
    color: inherit;
  }
  .room:hover { background: var(--surface3); border-color: var(--border2); }
  .room.active {
    background: color-mix(in oklab, var(--atomizer) 12%, var(--surface2));
    border-color: color-mix(in oklab, var(--atomizer) 50%, var(--border));
  }
  .row1 { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .id     { font-size: 11px; color: var(--atomizer); letter-spacing: 0.5px; }
  .status { font-size: 9px; color: var(--text3); text-transform: uppercase; letter-spacing: 1px; }
  .topic  { font-size: 11px; color: var(--text2); line-height: 1.4; overflow: hidden; text-overflow: ellipsis; }
</style>
