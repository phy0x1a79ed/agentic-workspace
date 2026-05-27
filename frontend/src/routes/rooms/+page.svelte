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
  import Button from '$lib/primitives/Button.svelte';
  import Input from '$lib/primitives/Input.svelte';
  import PanelLabel from '$lib/primitives/PanelLabel.svelte';
  import Select from '$lib/components/Select.svelte';

  const STATUS_OPTS = ['active', 'closed', 'archived', 'all'] as const;

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
      <div class="field">
        <PanelLabel>status</PanelLabel>
        <Select
          value={status}
          options={STATUS_OPTS}
          onchange={(v) => { status = v as typeof status; refresh(); }}
        />
      </div>
      <div class="field">
        <PanelLabel>peer</PanelLabel>
        <Select
          value={peer || 'local'}
          options={['local', ...peers, ...(peers.length ? ['all peers'] : [])]}
          onchange={(v) => {
            peer = v === 'local' ? '' : v === 'all peers' ? 'all' : v;
            refresh();
          }}
        />
      </div>
      <div class="field grow">
        <PanelLabel>search</PanelLabel>
        <div class="search-row">
          <div class="search-input">
            <Input type="search" placeholder="topic / transcript" bind:value={query} onkeydown={(e) => e.key === 'Enter' && doSearch()} />
          </div>
          <Button kind="ghost" onclick={doSearch}>search</Button>
          <Button kind="ghost" onclick={refresh}>refresh</Button>
        </div>
      </div>
    </div>
    <Button kind="primary" onclick={() => showNew = !showNew}>
      {showNew ? 'cancel' : '+ new room'}
    </Button>
  </header>

  {#if banner}<div class="banner">{banner}</div>{/if}

  {#if showNew}
    <form class="new-form" onsubmit={submitNew}>
      <label>
        <PanelLabel>topic</PanelLabel>
        <Input type="text" bind:value={newTopic} />
      </label>
      <label>
        <PanelLabel>scopes (comma-separated, e.g. <code>awm/dev,awm/api@crux</code>)</PanelLabel>
        <Input type="text" bind:value={newScopes} />
      </label>
      <label>
        <PanelLabel>prompts (JSON map scope→prompt)</PanelLabel>
        <textarea class="ta" rows="3" bind:value={newPrompts}></textarea>
      </label>
      <label class="cb">
        <input type="checkbox" bind:checked={newCloseOnExit} />
        <span>close_on_exit</span>
      </label>
      <div class="form-actions">
        <Button type="submit" kind="primary">create</Button>
        <Button kind="ghost" onclick={() => showNew = false}>cancel</Button>
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
                  <Button kind="ghost" onclick={() => goto(`${base}/room/${encodeURIComponent(r.id)}`)}>open</Button>
                  <Button kind="ghost" onclick={() => archive(r.id)}>archive</Button>
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
  .field { display: flex; flex-direction: column; gap: 4px; min-width: 140px; }
  .field.grow { flex: 1; min-width: 220px; }
  .search-row { display: flex; gap: 6px; align-items: stretch; }
  .search-input { flex: 1; display: flex; }
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
  .new-form .ta   {
    width: 100%;
    padding: var(--space-2) var(--space-4);
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    outline: none;
    resize: vertical;
    transition: border-color 120ms var(--ease-mech);
  }
  .new-form .ta:focus { border-color: var(--atomizer); }
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
