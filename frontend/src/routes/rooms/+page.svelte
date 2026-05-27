<script lang="ts">
  import { onMount } from 'svelte';
  import {
    listRooms, searchRooms, createRoom, archiveRoom, listPeers,
    type Room
  } from '$lib/api/client';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import StatusTag from '$lib/components/StatusTag.svelte';
  import RoomCard from '$lib/components/RoomCard.svelte';

  let status = $state<'active' | 'closed' | 'archived' | 'all'>('active');
  let peer   = $state<string>('');
  let query  = $state<string>('');
  let peers  = $state<string[]>([]);
  let rooms  = $state<Room[]>([]);
  let banner = $state<string>('');
  let loading = $state(false);

  let showNew = $state(false);
  let newTopic = $state('');
  let newScopes = $state('');
  let newPrompts = $state('{}');
  let newCloseOnExit = $state(false);

  onMount(() => {
    refresh();
    listPeers().then(r => { peers = r.peers.map(p => p.peer_id); }).catch(() => {});
  });

  async function refresh() {
    loading = true; banner = '';
    try { rooms = (await listRooms(status, peer)).rooms; }
    catch (e) { banner = (e as Error).message; rooms = []; }
    finally { loading = false; }
  }

  async function doSearch() {
    if (!query.trim()) { refresh(); return; }
    loading = true; banner = '';
    try {
      const r = await searchRooms(query.trim(), peer);
      rooms = r.rooms;
      if (r.degraded && r.degraded.length) {
        banner = `search complete; degraded peers: ${r.degraded.map(d => d.peer_id).join(', ')}`;
      }
    } catch (e) { banner = (e as Error).message; rooms = []; }
    finally { loading = false; }
  }

  async function submitNew(e: Event) {
    e.preventDefault();
    const scopes = newScopes.trim() ? newScopes.split(',').map(s => s.trim()).filter(Boolean) : [];
    let prompts: Record<string, string> = {};
    try { prompts = JSON.parse(newPrompts || '{}'); }
    catch { banner = 'prompts: invalid JSON'; return; }
    try {
      const r = await createRoom({
        topic: newTopic || null,
        scopes, prompts,
        close_on_exit: newCloseOnExit
      });
      showNew = false;
      banner = `created ${r.id}`;
      newTopic = ''; newScopes = ''; newPrompts = '{}'; newCloseOnExit = false;
      refresh();
    } catch (err) { banner = (err as Error).message; }
  }

  async function archive(id: string) {
    try { await archiveRoom(id); refresh(); }
    catch (e) { banner = (e as Error).message; }
  }
</script>

<div class="rooms">
  <header class="filter-bar">
    <div class="filters">
      <label class="field">
        <span class="panel-label">status</span>
        <select class="input mono-input" bind:value={status} onchange={refresh}>
          <option value="active">active</option>
          <option value="closed">closed</option>
          <option value="archived">archived</option>
          <option value="all">all</option>
        </select>
      </label>
      <label class="field">
        <span class="panel-label">peer</span>
        <select class="input mono-input" bind:value={peer} onchange={refresh}>
          <option value="">local</option>
          {#each peers as p}
            <option value={p}>{p}</option>
          {/each}
          {#if peers.length}<option value="all">all peers</option>{/if}
        </select>
      </label>
      <label class="field grow">
        <span class="panel-label">search</span>
        <div class="search-row">
          <input class="input" type="search" placeholder="topic / transcript" bind:value={query} onkeydown={(e) => e.key === 'Enter' && doSearch()} />
          <button class="btn ghost" onclick={doSearch}>search</button>
          <button class="btn ghost" onclick={refresh}>refresh</button>
        </div>
      </label>
    </div>
    <button class="btn primary" onclick={() => showNew = !showNew}>
      {showNew ? 'cancel' : '+ new room'}
    </button>
  </header>

  {#if banner}<div class="banner">{banner}</div>{/if}

  {#if showNew}
    <form class="new-form" onsubmit={submitNew}>
      <label>
        <span class="panel-label">topic</span>
        <input class="input" type="text" bind:value={newTopic} />
      </label>
      <label>
        <span class="panel-label">scopes (comma-separated, e.g. <code>awm/dev,awm/api@crux</code>)</span>
        <input class="input" type="text" bind:value={newScopes} />
      </label>
      <label>
        <span class="panel-label">prompts (JSON map scope→prompt)</span>
        <textarea class="input ta" rows="3" bind:value={newPrompts}></textarea>
      </label>
      <label class="cb">
        <input type="checkbox" bind:checked={newCloseOnExit} />
        <span>close_on_exit</span>
      </label>
      <div class="form-actions">
        <button type="submit" class="btn primary">create</button>
        <button type="button" class="btn ghost" onclick={() => showNew = false}>cancel</button>
      </div>
    </form>
  {/if}

  <section class="list">
    {#if loading}
      <div class="empty"><span class="mono dim">loading…</span></div>
    {:else if rooms.length === 0}
      <div class="empty-state">
        <div class="empty-icon">◇</div>
        <div class="empty-text">no rooms<br />try a different filter or create one</div>
      </div>
    {:else}
      <div class="table-scroll desktop">
        <table class="data">
          <thead><tr><th>id</th><th>topic</th><th>status</th><th>host</th><th>created</th><th></th></tr></thead>
          <tbody>
            {#each rooms as r}
              <tr>
                <td class="val">{r.id}</td>
                <td class="val">{r.topic ?? ''}</td>
                <td><StatusTag status={r.status} /></td>
                <td>{r.host_peer_id ?? ''}</td>
                <td>{r.created_at ?? ''}</td>
                <td class="row-actions">
                  <button class="btn ghost" onclick={() => goto(`${base}/room/${encodeURIComponent(r.id)}`)}>open</button>
                  <button class="btn ghost" onclick={() => archive(r.id)}>archive</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <div class="cards mobile">
        {#each rooms as r}
          <RoomCard room={r} onchange={refresh} />
        {/each}
      </div>
    {/if}
  </section>
</div>

<style>
  .rooms {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    padding: 16px 18px;
    gap: 14px;
    max-width: 1200px;
    width: 100%;
    align-self: center;
  }
  .filter-bar {
    display: flex;
    gap: 12px;
    align-items: flex-end;
    flex-wrap: wrap;
  }
  .filters {
    display: flex;
    gap: 12px;
    flex: 1;
    flex-wrap: wrap;
  }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .field.grow { flex: 1; min-width: 220px; }
  .mono-input { font-family: var(--mono); font-size: 12px; min-height: unset; }
  .search-row { display: flex; gap: 6px; }
  .search-row .input { flex: 1; }
  .banner {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text2);
    padding: 8px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
  }
  .new-form {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .new-form label { display: flex; flex-direction: column; gap: 4px; }
  .new-form code  { font-family: var(--mono); font-size: 10px; color: var(--text2); }
  .new-form .ta   { font-family: var(--mono); font-size: 11px; resize: vertical; }
  .new-form .cb   { flex-direction: row; align-items: center; gap: 8px; color: var(--text2); font-family: var(--mono); font-size: 11px; }
  .form-actions   { display: flex; gap: 8px; }

  .list  { flex: 1; overflow-y: auto; min-height: 0; }
  .empty { padding: 14px; }
  .mono  { font-family: var(--mono); }
  .dim   { color: var(--text3); }

  .row-actions { display: flex; gap: 6px; }
  .cards { display: none; flex-direction: column; gap: 10px; }

  @media (max-width: 720px) {
    .desktop { display: none; }
    .cards   { display: flex; }
    .rooms   { padding: 12px; }
    .filter-bar { gap: 8px; }
    .filters { gap: 8px; }
    .field   { flex: 1 1 100%; }
    .field.grow { flex: 1 1 100%; }
  }
</style>
