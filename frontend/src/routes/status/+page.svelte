<script lang="ts">
  import { onMount } from 'svelte';
  import {
    peerInfo, listPeers, listProjects, listScopes, coreStatus, pingPeer,
    type Peer, type Project, type Scope, type CoreStatus
  } from '$lib/api/client';
  import Button from '$lib/primitives/Button.svelte';
  import PanelLabel from '$lib/primitives/PanelLabel.svelte';

  let whoami     = $state<string>('loading…');
  let peers      = $state<Peer[]>([]);
  let peersErr   = $state<string>('');
  let projects   = $state<Project[]>([]);
  let projErr    = $state<string>('');
  let scopes     = $state<Scope[]>([]);
  let scopesErr  = $state<string>('');
  let core       = $state<string>('loading…');
  let pingResult = $state<Record<string, string>>({});

  onMount(() => { refreshAll(); });

  function refreshAll() {
    refreshWhoami();
    refreshPeers();
    refreshProjects();
    refreshScopes();
    refreshCore();
  }

  async function refreshWhoami() {
    try { whoami = JSON.stringify(await peerInfo(), null, 2); }
    catch (e) { whoami = (e as Error).message; }
  }

  async function refreshPeers() {
    peersErr = '';
    try { peers = (await listPeers()).peers; }
    catch (e) { peersErr = (e as Error).message; peers = []; }
  }

  async function refreshProjects() {
    projErr = '';
    try { projects = (await listProjects()).projects; }
    catch (e) { projErr = (e as Error).message; projects = []; }
  }

  async function refreshScopes() {
    scopesErr = '';
    try { scopes = (await listScopes('active')).scopes; }
    catch (e) { scopesErr = (e as Error).message; scopes = []; }
  }

  async function refreshCore() {
    try { core = JSON.stringify(await coreStatus(), null, 2); }
    catch (e) { core = (e as Error).message; }
  }

  async function ping(id: string) {
    pingResult[id] = '…';
    try {
      const r = await pingPeer(id);
      pingResult[id] = r.ok ? `ok ${r.rtt_ms}ms` : `ERR ${r.error ?? ''}`;
    } catch (e) { pingResult[id] = (e as Error).message; }
  }
</script>

<div class="status">
  <section>
    <header class="sec-header">
      <PanelLabel>whoami</PanelLabel>
      <Button kind="ghost" onclick={refreshWhoami}>refresh</Button>
    </header>
    <pre class="block">{whoami}</pre>
  </section>

  <section>
    <header class="sec-header">
      <PanelLabel>peers</PanelLabel>
      <Button kind="ghost" onclick={refreshPeers}>refresh</Button>
    </header>
    {#if peersErr}
      <div class="err">{peersErr}</div>
    {:else}
      <div class="table-scroll">
        <table class="data">
          <thead><tr><th>peer</th><th>name</th><th>ssh</th><th>port</th><th>last seen</th><th></th></tr></thead>
          <tbody>
            {#each peers as p}
              <tr>
                <td class="val">{p.peer_id}</td>
                <td>{p.friendly_name ?? ''}</td>
                <td>{p.ssh_alias ?? ''}</td>
                <td>{p.remote_port ?? ''}</td>
                <td>{p.last_seen ?? ''}</td>
                <td>
                  <Button kind="ghost" onclick={() => ping(p.peer_id)}>ping</Button>
                  <span class="ping">{pingResult[p.peer_id] ?? ''}</span>
                </td>
              </tr>
            {/each}
            {#if !peers.length}
              <tr><td colspan="6"><em>no peers</em></td></tr>
            {/if}
          </tbody>
        </table>
      </div>
    {/if}
  </section>

  <section>
    <header class="sec-header">
      <PanelLabel>projects</PanelLabel>
      <Button kind="ghost" onclick={refreshProjects}>refresh</Button>
    </header>
    {#if projErr}
      <div class="err">{projErr}</div>
    {:else if projects.length === 0}
      <div class="empty">no projects</div>
    {:else}
      <ul class="proj-list">
        {#each projects as p}
          <li>
            <span class="val">{p.name}</span>
            <span class="mono dim">active={p.scope_counts.active} completed={p.scope_counts.completed} deleted={p.scope_counts.deleted}</span>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section>
    <header class="sec-header">
      <PanelLabel>active scopes</PanelLabel>
      <Button kind="ghost" onclick={refreshScopes}>refresh</Button>
    </header>
    {#if scopesErr}
      <div class="err">{scopesErr}</div>
    {:else}
      <div class="table-scroll">
        <table class="data">
          <thead><tr><th>project</th><th>scope</th><th>branch</th><th>status</th><th>worktree</th></tr></thead>
          <tbody>
            {#each scopes as s}
              <tr>
                <td class="val">{s.project}</td>
                <td class="val">{s.scope}</td>
                <td>{s.branch}</td>
                <td>{s.status}</td>
                <td class="mono dim">{s.worktree}</td>
              </tr>
            {/each}
            {#if !scopes.length}
              <tr><td colspan="5"><em>no active scopes</em></td></tr>
            {/if}
          </tbody>
        </table>
      </div>
    {/if}
  </section>

  <section>
    <header class="sec-header">
      <PanelLabel>core</PanelLabel>
      <Button kind="ghost" onclick={refreshCore}>refresh</Button>
    </header>
    <pre class="block">{core}</pre>
  </section>
</div>

<style>
  .status {
    flex: 1;
    overflow-y: auto;
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 22px;
    max-width: 1100px;
    width: 100%;
    align-self: center;
  }
  section { background: var(--surface); border: 1px solid var(--border); border-radius: 4px; }
  .sec-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
  }
  .block {
    padding: 12px 14px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text2);
    white-space: pre-wrap;
    word-break: break-word;
    overflow-x: auto;
  }
  .err {
    padding: 12px 14px;
    color: var(--danger);
    font-family: var(--mono);
    font-size: 11px;
  }
  .empty {
    padding: 12px 14px;
    color: var(--text3);
    font-family: var(--mono);
    font-size: 11px;
  }
  .proj-list { list-style: none; padding: 0; margin: 0; }
  .proj-list li {
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .proj-list li:last-child { border-bottom: 0; }
  .mono { font-family: var(--mono); font-size: 10px; }
  .dim  { color: var(--text3); }
  .ping { font-family: var(--mono); font-size: 10px; color: var(--text2); margin-left: 6px; }

  @media (max-width: 720px) {
    .status { padding: 10px; gap: 14px; }
  }
</style>
