<script lang="ts">
  import { onMount } from 'svelte';
  import type { RoomAgent } from '$lib/api/client';
  import {
    listVoiceEngines, getGlobalEngines, putGlobalEngine,
    type EnginesByKind, type GlobalEnginesByKind,
  } from '$lib/api/client';
  import AgentList from './AgentList.svelte';
  import DynamicConfigForm from './DynamicConfigForm.svelte';
  import PanelLabel from '$lib/primitives/PanelLabel.svelte';
  import CollapsibleSection from '$lib/primitives/CollapsibleSection.svelte';
  import Button from '$lib/primitives/Button.svelte';

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

  // ── Global engine selection (STT + TTS) ──────────────────────────────
  // Engines are server-global: one running stt/tts at a time, persisted
  // in awm's config table. The drawer below is the only place these are
  // edited; it shows up identically inside every room's DetailsPanel.
  const KINDS = ['stt', 'tts'] as const;
  type Kind = (typeof KINDS)[number];

  let engines = $state<EnginesByKind | null>(null);
  let globalSel = $state<GlobalEnginesByKind>({ stt: null, tts: null });
  let draftId = $state<Record<Kind, string>>({ stt: '', tts: '' });
  let draftCfg = $state<Record<Kind, Record<string, unknown>>>({ stt: {}, tts: {} });
  let applying = $state<string>('');
  let loadErr = $state<string>('');

  onMount(async () => {
    try {
      const [list, sel] = await Promise.all([listVoiceEngines(), getGlobalEngines()]);
      engines = list;
      globalSel = sel;
      for (const kind of KINDS) {
        const cur = sel[kind];
        if (cur) {
          draftId[kind] = cur.engine_id;
          draftCfg[kind] = { ...cur.params };
        }
      }
    } catch (e) {
      loadErr = (e as Error).message;
    }
  });

  function engineIds(kind: Kind): string[] {
    return Object.keys(engines?.[kind] ?? {});
  }

  function currentSchema(kind: Kind) {
    const id = draftId[kind] || globalSel[kind]?.engine_id;
    if (!id) return null;
    return engines?.[kind]?.[id]?.schema ?? null;
  }

  function pickEngine(kind: Kind, id: string) {
    const same = draftId[kind] === id;
    draftId[kind] = id;
    if (!same) {
      draftCfg[kind] = { ...(engines?.[kind]?.[id]?.defaults ?? {}) };
    }
  }

  async function applyEngine(kind: Kind) {
    const id = draftId[kind];
    if (!id) return;
    applying = kind;
    try {
      globalSel = await putGlobalEngine(kind, id, draftCfg[kind] ?? {});
    } catch (e) {
      onagentresult?.((e as Error).message);
    } finally {
      applying = '';
    }
  }
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

  <CollapsibleSection label="engines" rail="none" compact>
    {#if loadErr}
      <div class="err mono">{loadErr}</div>
    {/if}
    {#each KINDS as kind (kind)}
      <div class="row">
        <div class="row-head">
          <span class="kind mono">{kind}</span>
          <div class="engine-buttons">
            {#each engineIds(kind) as eid (eid)}
              {@const active = (draftId[kind] || globalSel[kind]?.engine_id) === eid}
              <Button
                kind={active ? 'primary' : 'ghost'}
                disabled={applying === kind}
                onclick={() => pickEngine(kind, eid)}
              >{eid}</Button>
            {/each}
          </div>
          <Button
            kind="primary"
            disabled={applying === kind || !draftId[kind]}
            onclick={() => applyEngine(kind)}
          >{applying === kind ? '…' : 'apply'}</Button>
        </div>
        {#if currentSchema(kind)}
          <DynamicConfigForm
            schema={currentSchema(kind)}
            bind:value={draftCfg[kind]}
            disabled={applying === kind}
          />
        {/if}
      </div>
    {/each}
  </CollapsibleSection>
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
  .row { display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-1) 0; }
  .row + .row { border-top: 1px solid var(--border); }
  .row-head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
  }
  .kind {
    text-transform: uppercase;
    color: var(--text2);
    font-size: 11px;
    min-width: 28px;
  }
  .engine-buttons { display: flex; flex-wrap: wrap; gap: var(--space-1); flex: 1 1 auto; }
  .err { color: var(--danger); padding: var(--space-1) 0; font-size: 11px; }
  .mono { font-family: var(--mono); }
</style>
