<script lang="ts">
  import type { RoomAgent } from '$lib/api/client';
  import AgentCard from './AgentCard.svelte';
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
  function recipientKey(scope: string): string {
    return `scope:${scope}`;
  }
  function isRecipient(scope: string): boolean {
    return recipients.includes(recipientKey(scope));
  }
  function toggleRecipient(scope: string) {
    const key = recipientKey(scope);
    const set = new Set(recipients);
    if (set.has(key)) set.delete(key); else set.add(key);
    onrecipients?.([...set]);
  }

  let expanded = $state<Record<string, boolean>>({});
</script>

<div class="list">
  {#if agents.length === 0}
    <em class="dim mono">no agent participants</em>
  {:else}
    {#each agents as a (a.scope)}
      <AgentCard
        agent={a}
        {roomId}
        manager={isManager(a.scope)}
        recipient={isRecipient(a.scope)}
        bind:expanded={expanded[a.scope]}
        onToggleRecipient={() => toggleRecipient(a.scope)}
        {onresult}
      />
    {/each}
  {/if}
</div>

<style>
  .list { display: flex; flex-direction: column; gap: 6px; }
  em.dim { color: var(--text3); font-style: italic; padding: 6px 4px; font-size: 11px; }
  .mono { font-family: var(--mono); }
</style>
