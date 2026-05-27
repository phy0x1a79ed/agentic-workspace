<script lang="ts">
  import type { RoomAgent } from '$lib/api/client';
  import StatusTag from './StatusTag.svelte';

  interface Props { agents: RoomAgent[] }
  let { agents }: Props = $props();
</script>

<div class="list">
  {#if agents.length === 0}
    <em class="dim">no agent participants</em>
  {:else}
    {#each agents as a}
      <article class="card">
        <header>
          <span class="scope">{a.scope}</span>
          <span class="kind">{a.kind}</span>
        </header>
        {#if a.live}
          <div class="row mono">
            <StatusTag status={a.live.status ?? 'idle'} />
            <span class="dim">{a.live.agent_cli ?? '?'}</span>
            <span class="dim">pid {a.live.pid}</span>
          </div>
          <div class="row mono dim small">
            mode={a.live.permission_mode ?? '?'} model={a.live.model ?? 'default'} effort={a.live.effort ?? 'default'}
          </div>
          {#if a.live.started_at}
            <div class="row mono dim small">started {a.live.started_at}</div>
          {/if}
          {#if a.live.exited_at}
            <div class="row mono dim small">exited {a.live.exited_at} code={a.live.exit_code}</div>
          {/if}
        {:else}
          <div class="row mono dim small">
            {a.kind === 'shadow_peer' ? 'remote (peer)' : 'no live session'}
          </div>
        {/if}
      </article>
    {/each}
  {/if}
</div>

<style>
  .list  { display: flex; flex-direction: column; gap: 6px; }
  .card {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 8px 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  header { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
  .scope { font-family: var(--mono); font-size: 12px; color: var(--text); }
  .kind  { font-family: var(--mono); font-size: 9px; color: var(--text3); letter-spacing: 1px; text-transform: uppercase; }
  .row   { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .mono  { font-family: var(--mono); font-size: 11px; }
  .dim   { color: var(--text3); }
  .small { font-size: 10px; }
  em.dim { font-style: italic; padding: 6px 4px; }
</style>
