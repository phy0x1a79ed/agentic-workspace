<script lang="ts">
  import type { RoomAgent } from '$lib/api/client';
  import { runAgentSlash, ApiError } from '$lib/api/client';
  import StatusTag from './StatusTag.svelte';
  import AgentControls from './AgentControls.svelte';
  import Card from '$lib/primitives/Card.svelte';
  import Pill from '$lib/primitives/Pill.svelte';
  import PanelLabel from '$lib/primitives/PanelLabel.svelte';
  import Tooltip from '$lib/primitives/Tooltip.svelte';
  import { agentDisplayName, agentProjectLabel } from '$lib/utils/scope';

  type Rail = 'manager' | 'peer' | 'plain' | 'none';

  interface Props {
    agent: RoomAgent;
    roomId?: string | null;
    manager?: boolean;
    recipient?: boolean;
    expanded?: boolean;
    onToggleRecipient?: () => void;
    onresult?: (msg: string) => void;
  }
  let {
    agent,
    roomId = null,
    manager = false,
    recipient = false,
    expanded = $bindable(false),
    onToggleRecipient,
    onresult,
  }: Props = $props();

  const controllable = $derived(agent.kind === 'scope');

  let committing = $state(false);
  let busyAction = $state<string | null>(null);

  function flashRail() {
    committing = true;
    setTimeout(() => { committing = false; }, 250);
  }

  async function fire(cmd: string) {
    if (!roomId || busyAction) return;
    busyAction = cmd;
    try {
      const r = await runAgentSlash(roomId, agent.scope, cmd);
      onresult?.(r.result || `${cmd} ok`);
    } catch (e) {
      onresult?.(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      busyAction = null;
    }
  }

  const rail: Rail = $derived(
    manager ? 'manager'
    : agent.kind === 'shadow_peer' ? 'peer'
    : 'plain'
  );
  const kindTone: 'atomizer' | 'peer' | 'dim' = $derived(
    manager ? 'atomizer'
    : agent.kind === 'shadow_peer' ? 'peer'
    : 'dim'
  );

  const displayName = $derived(agentDisplayName(agent.scope));
  const projectLabel = $derived(agentProjectLabel(agent.scope));
</script>

<Card {rail} flash={committing} open={expanded}>
  <header class="head">
    <div class="meta">
      <button
        class="bracket"
        type="button"
        aria-pressed={recipient}
        aria-label="recipient toggle"
        title="send messages to this scope"
        onclick={() => onToggleRecipient?.()}
      >[{recipient ? 'x' : ' '}]</button>

      {#if projectLabel}
        <PanelLabel>{projectLabel}</PanelLabel>
      {/if}

      <span class="status">
        {#if agent.live?.status}
          <StatusTag status={agent.live.status} />
        {/if}
      </span>

      <span class="spacer"></span>

      {#if controllable && roomId}
        <span class="hdr-actions">
          <Pill
            disabled={!!busyAction}
            title="not supported in headless mode"
            onclick={() => fire('/compact')}
          >{busyAction === '/compact' ? '…' : 'compact'}</Pill>
          <Pill
            disabled={!!busyAction}
            title="wipe conversation context"
            onclick={() => fire('/clear')}
          >{busyAction === '/clear' ? '…' : 'clear'}</Pill>
        </span>
      {/if}

      <button
        class="chev"
        type="button"
        aria-label={expanded ? 'collapse' : 'expand'}
        onclick={() => (expanded = !expanded)}
      >
        <span class="chev-glyph" class:open={expanded}>▸</span>
      </button>
    </div>

    <button
      class="ident"
      type="button"
      onclick={() => (expanded = !expanded)}
      aria-expanded={expanded}
      aria-controls="agent-body-{agent.scope}"
    >
      <Tooltip content={agent.scope}>
        {#snippet trigger({ props })}
          <span class="name mono" {...props}>{displayName}</span>
        {/snippet}
      </Tooltip>
      {#if manager}
        <PanelLabel tone="atomizer">manager</PanelLabel>
      {:else if !controllable}
        <PanelLabel tone={kindTone}>{agent.kind}</PanelLabel>
      {/if}
    </button>
  </header>

  {#if expanded}
    <div class="body" id="agent-body-{agent.scope}">
      <div class="stats">
        {#if agent.live}
          <div class="srow"><span class="k"><PanelLabel>pid</PanelLabel></span><span class="v">{agent.live.pid ?? '?'}</span></div>
          <div class="srow"><span class="k"><PanelLabel>cli</PanelLabel></span><span class="v">{agent.live.agent_cli ?? '?'}</span></div>
          {#if agent.live.started_at}
            <div class="srow"><span class="k"><PanelLabel>started</PanelLabel></span><span class="v">{agent.live.started_at}</span></div>
          {/if}
          {#if agent.live.exited_at}
            <div class="srow"><span class="k"><PanelLabel>exited</PanelLabel></span><span class="v">{agent.live.exited_at} (code {agent.live.exit_code})</span></div>
          {/if}
        {:else}
          <div class="srow"><span class="k"><PanelLabel>state</PanelLabel></span><span class="v dim">{agent.kind === 'shadow_peer' ? 'remote (peer)' : 'no live session'}</span></div>
        {/if}
      </div>

      {#if controllable && roomId}
        <AgentControls
          {roomId}
          scope={agent.scope}
          live={agent.live ?? null}
          onresult={onresult}
          oncommitted={flashRail}
        />
      {/if}
    </div>
  {/if}
</Card>

<style>
  .mono { font-family: var(--mono); }

  .head {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }
  .meta {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-height: 24px;
  }
  .spacer { flex: 1 1 auto; min-width: 0; }

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

  .ident {
    background: transparent;
    border: 0;
    color: inherit;
    padding: var(--space-0) 0;
    cursor: pointer;
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--space-3);
    width: 100%;
    text-align: left;
  }
  .ident:hover .name { color: var(--text); }
  .name {
    font-size: 14px;
    color: var(--text);
    line-height: 1.2;
    white-space: normal;
    word-break: break-word;
    transition: color 140ms var(--ease-mech);
  }

  .status { display: inline-flex; align-items: center; }
  .hdr-actions { display: inline-flex; gap: var(--space-1); }

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
    .meta { gap: var(--space-1); }
    .bracket, .chev { min-height: 32px; display: inline-flex; align-items: center; }
  }
</style>
