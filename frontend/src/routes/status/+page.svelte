<script lang="ts">
  import { onMount } from 'svelte';
  import {
    peerInfo, listPeers, listProjects, listScopes, coreStatus, pingPeer,
    type Peer, type Project, type Scope, type CoreStatus,
  } from '$lib/api/client';
  import Button from '$lib/primitives/Button.svelte';
  import CollapsibleSection from '$lib/primitives/CollapsibleSection.svelte';
  import ConfigStats, { type StatRow } from '$lib/components/ConfigStats.svelte';

  // ── Status (existing stats) ────────────────────────────────────────────
  let whoami     = $state<Peer | null>(null);
  let whoamiErr  = $state<string>('');
  let peers      = $state<Peer[]>([]);
  let peersErr   = $state<string>('');
  let projects   = $state<Project[]>([]);
  let projErr    = $state<string>('');
  let scopes     = $state<Scope[]>([]);
  let scopesErr  = $state<string>('');
  let core       = $state<CoreStatus | null>(null);
  let coreErr    = $state<string>('');
  let pingResult = $state<Record<string, string>>({});

  // Sections open by default: only Status, to keep the page short on first paint.
  let statusOpen = $state(true);
  let agentsOpen = $state(false);

  onMount(() => { refreshAll(); });

  function refreshAll() {
    refreshWhoami();
    refreshPeers();
    refreshProjects();
    refreshScopes();
    refreshCore();
  }

  async function refreshWhoami() {
    whoamiErr = '';
    try { whoami = await peerInfo(); }
    catch (e) { whoamiErr = (e as Error).message; whoami = null; }
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
    coreErr = '';
    try { core = await coreStatus(); }
    catch (e) { coreErr = (e as Error).message; core = null; }
  }

  async function ping(id: string) {
    pingResult[id] = '…';
    try {
      const r = await pingPeer(id);
      pingResult[id] = r.ok ? `ok ${r.rtt_ms}ms` : `ERR ${r.error ?? ''}`;
    } catch (e) { pingResult[id] = (e as Error).message; }
  }

  // ── Row builders ───────────────────────────────────────────────────────
  function peerRows(p: Peer): StatRow[] {
    return [
      { label: 'peer', value: p.peer_id, mono: true },
      { label: 'name', value: p.friendly_name ?? '' },
      { label: 'ssh', value: p.ssh_alias ?? '' },
      { label: 'port', value: p.remote_port ?? '' },
      { label: 'last seen', value: p.last_seen ?? '' },
    ];
  }
  function projectRows(p: Project): StatRow[] {
    return [
      { label: 'name', value: p.name, mono: true },
      { label: 'active', value: p.scope_counts.active },
      { label: 'completed', value: p.scope_counts.completed, dim: true },
      { label: 'deleted', value: p.scope_counts.deleted, dim: true },
    ];
  }
  function scopeRows(s: Scope): StatRow[] {
    return [
      { label: 'project', value: s.project, mono: true },
      { label: 'scope', value: s.scope, mono: true },
      { label: 'branch', value: s.branch },
      { label: 'status', value: s.status },
      { label: 'worktree', value: s.worktree, mono: true, dim: true },
    ];
  }
  function coreRows(c: CoreStatus | null): StatRow[] {
    if (!c) return [];
    return Object.entries(c).map(([k, v]) => ({
      label: k,
      value: typeof v === 'object' ? JSON.stringify(v) : String(v),
      mono: typeof v !== 'object',
    }));
  }
</script>

<div class="control">
  <CollapsibleSection label="status" bind:open={statusOpen} rail="manager">
    {#snippet actions()}
      <Button kind="ghost" onclick={refreshAll}>refresh</Button>
    {/snippet}

    <CollapsibleSection label="whoami" rail="none" compact>
      {#if whoamiErr}
        <div class="err mono">{whoamiErr}</div>
      {:else if whoami}
        <ConfigStats rows={peerRows(whoami)} compact />
      {/if}
    </CollapsibleSection>

    <CollapsibleSection label="peers ({peers.length})" rail="none" compact>
      {#snippet actions()}
        <Button kind="ghost" onclick={refreshPeers}>refresh</Button>
      {/snippet}
      {#if peersErr}
        <div class="err mono">{peersErr}</div>
      {:else if !peers.length}
        <div class="empty mono">no peers</div>
      {:else}
        {#each peers as p (p.peer_id)}
          <div class="entry">
            <ConfigStats rows={peerRows(p)} compact />
            <div class="entry-actions">
              <Button kind="ghost" onclick={() => ping(p.peer_id)}>ping</Button>
              <span class="ping mono">{pingResult[p.peer_id] ?? ''}</span>
            </div>
          </div>
        {/each}
      {/if}
    </CollapsibleSection>

    <CollapsibleSection label="projects ({projects.length})" rail="none" compact>
      {#snippet actions()}
        <Button kind="ghost" onclick={refreshProjects}>refresh</Button>
      {/snippet}
      {#if projErr}
        <div class="err mono">{projErr}</div>
      {:else if !projects.length}
        <div class="empty mono">no projects</div>
      {:else}
        {#each projects as p (p.name)}
          <div class="entry">
            <ConfigStats rows={projectRows(p)} compact />
          </div>
        {/each}
      {/if}
    </CollapsibleSection>

    <CollapsibleSection label="active scopes ({scopes.length})" rail="none" compact>
      {#snippet actions()}
        <Button kind="ghost" onclick={refreshScopes}>refresh</Button>
      {/snippet}
      {#if scopesErr}
        <div class="err mono">{scopesErr}</div>
      {:else if !scopes.length}
        <div class="empty mono">no active scopes</div>
      {:else}
        {#each scopes as s (s.project + '/' + s.scope)}
          <div class="entry">
            <ConfigStats rows={scopeRows(s)} compact />
          </div>
        {/each}
      {/if}
    </CollapsibleSection>

    <CollapsibleSection label="core" rail="none" compact>
      {#snippet actions()}
        <Button kind="ghost" onclick={refreshCore}>refresh</Button>
      {/snippet}
      {#if coreErr}
        <div class="err mono">{coreErr}</div>
      {:else if !core}
        <div class="empty mono">no data</div>
      {:else}
        <ConfigStats rows={coreRows(core)} compact />
      {/if}
    </CollapsibleSection>
  </CollapsibleSection>

  <CollapsibleSection label="agents" bind:open={agentsOpen} rail="plain">
    <div class="hint mono">
      agent inventory and quick-actions land here next.
      for now use the right rail in /focus.
    </div>
  </CollapsibleSection>
</div>

<style>
  .control {
    flex: 1;
    overflow-y: auto;
    padding: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    width: 100%;
  }
  .entry {
    padding: var(--space-2) 0;
    border-bottom: 1px dashed var(--border);
  }
  .entry:last-child { border-bottom: 0; }
  .entry-actions {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding-top: var(--space-1);
  }
  .ping { font-size: 10px; color: var(--text2); }
  .err {
    padding: var(--space-2);
    color: var(--danger);
    font-size: 11px;
  }
  .empty {
    padding: var(--space-1) 0;
    color: var(--text3);
    font-size: 11px;
  }
  .hint {
    padding: var(--space-2);
    color: var(--text3);
    font-size: 10px;
    font-style: italic;
  }
  .mono { font-family: var(--mono); }

  @media (max-width: 720px) {
    .control { padding: var(--space-2); gap: var(--space-2); }
  }
</style>
