<script lang="ts">
  /**
   * Agent rail for the focus details panel. Each row is a single card that
   * bundles:
   *  - recipient toggle (bracket-glyph `[x]` / `[ ]`)
   *  - scope + kind + status (always visible)
   *  - compact / clear pills on the manager card (always visible)
   *  - expand chevron → collapsible body with stats grid + (manager-only)
   *    `AgentControls` form (mode/effort/model + restart + kill)
   *
   * A 3px left rail colored by kind (manager → atomizer, shadow_peer →
   * recording, other → text3) gives the cards a rack-unit feel. The rail
   * flashes warn-tinted for 250ms when the config form commits.
   */
  import type { RoomAgent } from '$lib/api/client';
  import { runAgentSlash, ApiError } from '$lib/api/client';
  import StatusTag from './StatusTag.svelte';
  import AgentControls from './AgentControls.svelte';
  import { ui } from '$lib/state/ui.svelte';

  interface Props {
    agents: RoomAgent[];
    roomId?: string | null;
    recipients?: string[];
    onrecipients?: (keys: string[]) => void;
    onresult?: (msg: string) => void;
  }
  let {
    agents,
    roomId = null,
    recipients = [],
    onrecipients,
    onresult,
  }: Props = $props();

  function isManager(scope: string): boolean {
    return !!ui.managerScope && ui.managerScope === scope;
  }
  // Any scope-kind agent in this room is controllable (compact/clear/restart/
  // kill, set mode/model/effort). The user's own manager just gets a
  // different rail color via isManager() — the action set is identical.
  function isControllable(a: RoomAgent): boolean {
    return a.kind === 'scope';
  }
  function recipientKey(scope: string): string {
    return `scope:${scope}`;
  }

  let expanded   = $state<Record<string, boolean>>({});
  let committing = $state<Record<string, boolean>>({});
  let busyAction = $state<Record<string, string | null>>({});

  function toggleExpand(scope: string) {
    expanded[scope] = !expanded[scope];
  }
  function toggleRecipient(scope: string) {
    const key = recipientKey(scope);
    const set = new Set(recipients);
    if (set.has(key)) set.delete(key); else set.add(key);
    onrecipients?.([...set]);
  }
  function isRecipient(scope: string): boolean {
    return recipients.includes(recipientKey(scope));
  }

  async function fire(scope: string, cmd: string) {
    if (!roomId) return;
    const tag = `${scope}:${cmd}`;
    if (busyAction[scope]) return;
    busyAction[scope] = cmd;
    try {
      const r = await runAgentSlash(roomId, scope, cmd);
      onresult?.(r.result || `${cmd} ok`);
    } catch (e) {
      onresult?.(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      busyAction[scope] = null;
    }
  }

  function flashRail(scope: string) {
    committing[scope] = true;
    setTimeout(() => { committing[scope] = false; }, 250);
  }

  function kindRailClass(a: RoomAgent): string {
    if (isManager(a.scope)) return 'rail-manager';
    if (a.kind === 'shadow_peer') return 'rail-peer';
    return 'rail-plain';
  }
</script>

<div class="list">
  {#if agents.length === 0}
    <em class="dim mono">no agent participants</em>
  {:else}
    {#each agents as a (a.scope)}
      {@const mgr = isManager(a.scope)}
      {@const ctl = isControllable(a)}
      {@const open = !!expanded[a.scope]}
      <article
        class="card {kindRailClass(a)}"
        class:committing={committing[a.scope]}
        class:open
      >
        <header class="row">
          <button
            class="bracket"
            type="button"
            aria-pressed={isRecipient(a.scope)}
            aria-label="recipient toggle"
            title="send messages to this scope"
            onclick={() => toggleRecipient(a.scope)}
          >[{isRecipient(a.scope) ? 'x' : ' '}]</button>

          <button
            class="ident"
            type="button"
            onclick={() => toggleExpand(a.scope)}
            aria-expanded={open}
            aria-controls="agent-body-{a.scope}"
          >
            <span class="scope mono">{a.scope}</span>
            {#if mgr}
              <span class="kind mono">manager</span>
            {:else if !ctl}
              <span class="kind mono">{a.kind}</span>
            {/if}
          </button>

          <span class="status">
            {#if a.live?.status}
              <StatusTag status={a.live.status} />
            {/if}
          </span>

          {#if ctl && roomId}
            <span class="hdr-actions">
              <button
                class="pill"
                type="button"
                disabled={!!busyAction[a.scope]}
                title="not supported in headless mode"
                onclick={() => fire(a.scope, '/compact')}
              >{busyAction[a.scope] === '/compact' ? '…' : 'compact'}</button>
              <button
                class="pill"
                type="button"
                disabled={!!busyAction[a.scope]}
                title="wipe conversation context"
                onclick={() => fire(a.scope, '/clear')}
              >{busyAction[a.scope] === '/clear' ? '…' : 'clear'}</button>
            </span>
          {/if}

          <button
            class="chev"
            type="button"
            aria-label={open ? 'collapse' : 'expand'}
            onclick={() => toggleExpand(a.scope)}
          >
            <span class="chev-glyph" class:open>▸</span>
          </button>
        </header>

        {#if open}
          <div class="body" id="agent-body-{a.scope}">
            <div class="stats">
              {#if a.live}
                <div class="srow"><span class="k">pid:</span><span class="v">{a.live.pid ?? '?'}</span></div>
                <div class="srow"><span class="k">cli:</span><span class="v">{a.live.agent_cli ?? '?'}</span></div>
                {#if a.live.started_at}
                  <div class="srow"><span class="k">started:</span><span class="v">{a.live.started_at}</span></div>
                {/if}
                {#if a.live.exited_at}
                  <div class="srow"><span class="k">exited:</span><span class="v">{a.live.exited_at} (code {a.live.exit_code})</span></div>
                {/if}
              {:else}
                <div class="srow"><span class="k">state:</span><span class="v dim">{a.kind === 'shadow_peer' ? 'remote (peer)' : 'no live session'}</span></div>
              {/if}
            </div>

            {#if ctl && roomId}
              <AgentControls
                {roomId}
                scope={a.scope}
                live={a.live ?? null}
                onresult={onresult}
                oncommitted={() => flashRail(a.scope)}
              />
            {/if}
          </div>
        {/if}
      </article>
    {/each}
  {/if}
</div>

<style>
  .list { display: flex; flex-direction: column; gap: 6px; }
  em.dim { color: var(--text3); font-style: italic; padding: 6px 4px; font-size: 11px; }
  .mono { font-family: var(--mono); }

  .card {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-left-width: 3px;
    border-radius: 4px;
    padding: 6px 10px 6px 8px;
    display: flex;
    flex-direction: column;
    transition: border-left-color 200ms ease, background 180ms ease;
  }
  .rail-manager { border-left-color: var(--atomizer); }
  .rail-peer    { border-left-color: var(--recording); }
  .rail-plain   { border-left-color: var(--text3); }
  .card.committing { border-left-color: var(--warn); }
  .card.open       { background: color-mix(in oklab, var(--surface3) 50%, var(--surface2)); }

  .row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto auto auto;
    align-items: center;
    gap: 6px;
    min-height: 28px;
  }

  /* recipient bracket toggle */
  .bracket {
    background: transparent;
    border: 0;
    color: var(--text3);
    font-family: var(--mono);
    font-size: 12px;
    padding: 4px 2px;
    cursor: pointer;
    letter-spacing: 0;
    transition: color 140ms ease;
  }
  .bracket[aria-pressed="true"] { color: var(--atomizer); }
  .bracket:hover { color: var(--text); }

  /* scope+kind clickable label */
  .ident {
    background: transparent;
    border: 0;
    color: inherit;
    padding: 2px 0;
    cursor: pointer;
    display: flex;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
    text-align: left;
  }
  .ident:hover .scope { color: var(--text); }
  .scope {
    font-size: 12px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    transition: color 140ms ease;
  }
  .kind {
    font-size: 9px;
    color: var(--text3);
    letter-spacing: 1px;
    text-transform: uppercase;
    flex-shrink: 0;
  }
  .card.rail-manager .kind { color: var(--atomizer); }
  .card.rail-peer    .kind { color: var(--recording); }

  .status { display: inline-flex; align-items: center; }

  .hdr-actions { display: inline-flex; gap: 4px; }
  .pill {
    background: var(--surface3);
    color: var(--text2);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 3px 8px;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 120ms ease, border-color 120ms ease, color 120ms ease;
  }
  .pill:hover:not(:disabled) {
    background: color-mix(in oklab, var(--atomizer) 18%, var(--surface3));
    border-color: color-mix(in oklab, var(--atomizer) 45%, var(--border));
    color: var(--text);
  }
  .pill:disabled { opacity: 0.5; cursor: default; }

  /* expand chevron — notched cubic-bezier rotate */
  .chev {
    background: transparent;
    border: 0;
    color: var(--text3);
    padding: 4px 2px 4px 6px;
    cursor: pointer;
    line-height: 1;
  }
  .chev:hover { color: var(--text2); }
  .chev-glyph {
    display: inline-block;
    font-family: var(--mono);
    font-size: 11px;
    transition: transform 160ms cubic-bezier(0.7, 0, 0.3, 1), color 140ms ease;
  }
  .chev-glyph.open { transform: rotate(90deg); }

  /* collapsible body */
  .body {
    padding: 6px 0 4px 0;
    display: flex;
    flex-direction: column;
  }
  .stats {
    display: grid;
    grid-template-columns: 1fr;
    gap: 2px;
    padding: 4px 0;
  }
  .srow {
    display: grid;
    grid-template-columns: 56px 1fr;
    gap: 8px;
    align-items: baseline;
    font-family: var(--mono);
    font-size: 10px;
  }
  .srow .k {
    color: var(--text3);
    letter-spacing: 1px;
    text-transform: uppercase;
    text-align: right;
  }
  .srow .v { color: var(--text2); word-break: break-all; }
  .srow .v.dim { color: var(--text3); font-style: italic; }

  @media (max-width: 720px) {
    .row { gap: 4px; }
    .bracket, .chev { min-height: 32px; display: inline-flex; align-items: center; }
    .pill { padding: 5px 8px; }
  }
</style>
