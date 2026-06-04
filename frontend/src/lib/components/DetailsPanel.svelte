<script lang="ts">
  import type { RoomAgent } from '$lib/api/client';
  import AgentList from './AgentList.svelte';
  import PanelLabel from '$lib/components/primitives/PanelLabel.svelte';

  interface Props {
    agents: RoomAgent[];
    recipients: string[];
    roomId?: string | null;
    onrecipients?: (keys: string[]) => void;
    onagentresult?: (msg: string) => void;
  }
  let {
    agents,
    recipients,
    roomId = null,
    onrecipients,
    onagentresult,
  }: Props = $props();
</script>

<aside class="panel">
  <header class="ph">
    <PanelLabel>agents</PanelLabel>
  </header>
  <AgentList
    {agents}
    {roomId}
    {recipients}
    onrecipients={(k) => onrecipients?.(k)}
    onresult={(m) => onagentresult?.(m)}
  />
</aside>

<style>
  .panel {
    background: var(--surface);
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px;
    overflow-y: auto;
    height: 100%;
  }
  .ph { display: flex; align-items: baseline; justify-content: space-between; }
</style>
