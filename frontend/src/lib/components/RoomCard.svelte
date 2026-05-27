<script lang="ts">
  import StatusTag from './StatusTag.svelte';
  import { archiveRoom, type Room } from '$lib/api/client';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';

  interface Props {
    room: Room;
    onchange?: () => void;
  }
  let { room, onchange }: Props = $props();

  let busy = $state(false);
  let err  = $state('');

  function open() { goto(`${base}/room/${encodeURIComponent(room.id)}`); }

  async function archive() {
    busy = true; err = '';
    try { await archiveRoom(room.id); onchange?.(); }
    catch (e) { err = (e as Error).message; }
    finally { busy = false; }
  }
</script>

<div class="card">
  <div class="row1">
    <span class="id mono">{room.id}</span>
    <StatusTag status={room.status} />
  </div>
  {#if room.topic}
    <div class="topic">{room.topic}</div>
  {/if}
  <div class="meta mono">
    {#if room.host_peer_id}<span>host: {room.host_peer_id}</span>{/if}
    {#if room.created_at}<span>{room.created_at}</span>{/if}
  </div>
  <div class="actions">
    <button class="btn primary" onclick={open}>open</button>
    <button class="btn ghost"   onclick={archive} disabled={busy}>archive</button>
    {#if err}<span class="err">{err}</span>{/if}
  </div>
</div>

<style>
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .row1 { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .id   { color: var(--atomizer); font-size: 11px; letter-spacing: 0.5px; }
  .mono { font-family: var(--mono); }
  .topic { color: var(--text); font-size: 14px; line-height: 1.4; }
  .meta { display: flex; gap: 12px; color: var(--text3); font-size: 10px; flex-wrap: wrap; }
  .actions { display: flex; gap: 8px; align-items: center; margin-top: 4px; }
  .err { color: var(--danger); font-family: var(--mono); font-size: 10px; }
</style>
