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
  import Card from '$lib/primitives/Card.svelte';
  import Pill from '$lib/primitives/Pill.svelte';
  import PanelLabel from '$lib/primitives/PanelLabel.svelte';
  import Tooltip from '$lib/primitives/Tooltip.svelte';

  type Rail = 'manager' | 'peer' | 'plain' | 'none';

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

  function kindRail(a: RoomAgent): Rail {
    if (isManager(a.scope)) return 'manager';
    if (a.kind === 'shadow_peer') return 'peer';
    return 'plain';
  }
  function kindTone(a: RoomAgent): 'atomizer' | 'peer' | 'dim' {
    if (isManager(a.scope)) return 'atomizer';
    if (a.kind === 'shadow_peer') return 'peer';
    return 'dim';
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
      <Card rail={kindRail(a)} flash={committing[a.scope]} {open}>
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
            <Tooltip content={a.scope}>
              {#snippet trigger({ props })}
                <span class="scope mono" {...props}>{a.scope}</span>
              {/snippet}
            </Tooltip>
            {#if mgr}
              <PanelLabel tone="atomizer">manager</PanelLabel>
            {:else if !ctl}
              <PanelLabel tone={kindTone(a)}>{a.kind}</PanelLabel>
            {/if}
          </button>

          <span class="status">
            {#if a.live?.status}
              <StatusTag status={a.live.status} />
            {/if}
          </span>

          {#if ctl && roomId}
            <span class="hdr-actions">
              <Pill
                disabled={!!busyAction[a.scope]}
                title="not supported in headless mode"
                onclick={() => fire(a.scope, '/compact')}
              >{busyAction[a.scope] === '/compact' ? '…' : 'compact'}</Pill>
              <Pill
                disabled={!!busyAction[a.scope]}
                title="wipe conversation context"
                onclick={() => fire(a.scope, '/clear')}
              >{busyAction[a.scope] === '/clear' ? '…' : 'clear'}</Pill>
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
                <div class="srow"><span class="k"><PanelLabel>pid</PanelLabel></span><span class="v">{a.live.pid ?? '?'}</span></div>
                <div class="srow"><span class="k"><PanelLabel>cli</PanelLabel></span><span class="v">{a.live.agent_cli ?? '?'}</span></div>
                {#if a.live.started_at}
                  <div class="srow"><span class="k"><PanelLabel>started</PanelLabel></span><span class="v">{a.live.started_at}</span></div>
                {/if}
                {#if a.live.exited_at}
                  <div class="srow"><span class="k"><PanelLabel>exited</PanelLabel></span><span class="v">{a.live.exited_at} (code {a.live.exit_code})</span></div>
                {/if}
              {:else}
                <div class="srow"><span class="k"><PanelLabel>state</PanelLabel></span><span class="v dim">{a.kind === 'shadow_peer' ? 'remote (peer)' : 'no live session'}</span></div>
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
      </Card>
    {/each}
  {/if}
</div>

<style>
  .list { display: flex; flex-direction: column; gap: 6px; }
  em.dim { color: var(--text3); font-style: italic; padding: 6px 4px; font-size: 11px; }
  .mono { font-family: var(--mono); }

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
    padding: var(--space-1) var(--space-0);
    cursor: pointer;
    letter-spacing: 0;
    transition: color 140ms var(--ease-mech);
  }
  .bracket[aria-pressed="true"] { color: var(--atomizer); }
  .bracket:hover { color: var(--text); }

  /* scope+kind clickable label */
  .ident {
    background: transparent;
    border: 0;
    color: inherit;
    padding: var(--space-0) 0;
    cursor: pointer;
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
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
    transition: color 140ms var(--ease-mech);
  }

  .status { display: inline-flex; align-items: center; }

  .hdr-actions { display: inline-flex; gap: var(--space-1); }

  /* expand chevron — notched cubic-bezier rotate */
  .chev {
    background: transparent;
    border: 0;
    color: var(--text3);
    padding: var(--space-1) var(--space-0) var(--space-1) var(--space-2);
    cursor: pointer;
    line-height: 1;
  }
  .chev:hover { color: var(--text2); }
  .chev-glyph {
    display: inline-block;
    font-family: var(--mono);
    font-size: 11px;
    transition: transform 160ms var(--ease-mech), color 140ms var(--ease-mech);
  }
  .chev-glyph.open { transform: rotate(90deg); }

  /* collapsible body */
  .body {
    padding: var(--space-2) 0 var(--space-1) 0;
    display: flex;
    flex-direction: column;
  }
  .stats {
    display: grid;
    grid-template-columns: 1fr;
    gap: var(--space-0);
    padding: var(--space-1) 0;
  }
  .srow {
    display: grid;
    grid-template-columns: 56px 1fr;
    gap: var(--space-3);
    align-items: baseline;
    font-family: var(--mono);
    font-size: 10px;
  }
  .srow .k { text-align: right; }
  .srow .v { color: var(--text2); word-break: break-all; }
  .srow .v.dim { color: var(--text3); font-style: italic; }

  @media (max-width: 720px) {
    .row { gap: var(--space-1); }
    .bracket, .chev { min-height: 32px; display: inline-flex; align-items: center; }
  }
</style>
