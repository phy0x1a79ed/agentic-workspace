<script lang="ts">
  import type { RoomAgent } from '$lib/api/client';
  import AgentList from './AgentList.svelte';
  import RecipientChips from './RecipientChips.svelte';

  interface Props {
    agents: RoomAgent[];
    recipients: string[];
    roomId?: string | null;
    onrecipients?: (keys: string[]) => void;
    onagentresult?: (msg: string) => void;
    voiceStage?: string;
    voiceConn?:  string;
    voiceMeter?: string;
  }
  let {
    agents,
    recipients,
    roomId = null,
    onrecipients,
    onagentresult,
    voiceStage = 'idle',
    voiceConn  = 'disconnected',
    voiceMeter = '—'
  }: Props = $props();
</script>

<aside class="panel">
  <section>
    <header class="ph"><span class="panel-label">agents</span></header>
    <AgentList {agents} {roomId} onresult={(m) => onagentresult?.(m)} />
  </section>

  <section>
    <header class="ph"><span class="panel-label">recipients</span></header>
    <RecipientChips {agents} selected={recipients} onchange={(k) => onrecipients?.(k)} />
  </section>

  <section>
    <header class="ph"><span class="panel-label">voice</span></header>
    <div class="voice mono">
      <div><span class="dim">conn:</span> <span>{voiceConn}</span></div>
      <div><span class="dim">stage:</span> <span>{voiceStage}</span></div>
      <div><span class="dim">mic:</span> <span>{voiceMeter}</span></div>
    </div>
  </section>
</aside>

<style>
  .panel {
    background: var(--surface);
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 14px;
    overflow-y: auto;
    height: 100%;
  }
  .ph { margin-bottom: 8px; }
  .voice { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--text); }
  .mono  { font-family: var(--mono); }
  .dim   { color: var(--text3); }
</style>
