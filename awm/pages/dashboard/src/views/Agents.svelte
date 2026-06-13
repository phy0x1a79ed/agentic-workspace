<script lang="ts">
  import { PanelLabel, Tag } from '@awm/primitives';
  import Skeleton from '../lib/Skeleton.svelte';

  // Illustrative roster ONLY — static placeholder data showing the intended
  // shape: top-level (parent) agents and the task agents they spawned. The
  // real roster comes from agents.parent_id once spawn logic populates it
  // (see tasks/CONTRACT.md); only the parent + its children are conversable.
  type Node = { scope: string; label: string; live: boolean; children: Node[] };
  const ROSTER: Node[] = [
    {
      scope: 'payments/orchestrator',
      label: 'orchestrator',
      live: true,
      children: [
        { scope: 'payments/ledger-migrate', label: 'task: ledger-migrate', live: false, children: [] },
        { scope: 'payments/webhook-retry', label: 'task: webhook-retry', live: true, children: [] }
      ]
    },
    {
      scope: 'infra/orchestrator',
      label: 'orchestrator',
      live: true,
      children: [{ scope: 'infra/cert-rotate', label: 'task: cert-rotate', live: true, children: [] }]
    }
  ];

  let selected = $state<string>('');
  let pane = $state<'roster' | 'convo'>('roster'); // mobile only

  function pick(scope: string) {
    selected = scope;
    pane = 'convo';
  }
</script>

<div class="agents" class:show-convo={pane === 'convo'}>
  <!-- roster -->
  <aside class="roster">
    <PanelLabel tone="dim">conversable agents</PanelLabel>
    <p class="hint">parent agents + the tasks they spawned</p>
    <ul class="tree">
      {#each ROSTER as parent (parent.scope)}
        <li>
          <button class="node parent" class:sel={selected === parent.scope} onclick={() => pick(parent.scope)}>
            <span class="dot" class:on={parent.live}></span>
            <span class="nlabel">{parent.label}</span>
            <span class="proj">{parent.scope.split('/')[0]}</span>
          </button>
          <ul class="kids">
            {#each parent.children as kid (kid.scope)}
              <li>
                <button class="node kid" class:sel={selected === kid.scope} onclick={() => pick(kid.scope)}>
                  <span class="dot" class:on={kid.live}></span>
                  <span class="nlabel">{kid.label}</span>
                </button>
              </li>
            {/each}
          </ul>
        </li>
      {/each}
    </ul>
  </aside>

  <!-- conversation -->
  <section class="convo">
    <header class="chead">
      <button class="back" onclick={() => (pane = 'roster')}>‹ agents</button>
      {#if selected}
        <span class="cscope">{selected}</span><Tag tone="atomizer">live</Tag>
      {:else}
        <span class="cscope muted">select an agent</span>
      {/if}
    </header>
    <div class="stream">
      <Skeleton label="tts-history (transcript)" height="100%" />
    </div>
    <div class="composer">
      <Skeleton label="ptt-composer (text / speech)" height="56px" />
    </div>
  </section>
</div>

<style>
  .agents { display: flex; flex-direction: column; height: 100%; }

  .roster { padding: var(--space-5); display: flex; flex-direction: column; gap: var(--space-2); }
  .hint { font-size: 10px; color: var(--text3); font-family: var(--mono); margin-bottom: var(--space-2); }
  .tree { list-style: none; display: flex; flex-direction: column; gap: var(--space-1); }
  .kids { list-style: none; display: flex; flex-direction: column; gap: var(--space-1); margin: var(--space-1) 0 var(--space-2) var(--space-5); }
  .node {
    display: flex; align-items: center; gap: var(--space-2); width: 100%;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius-md); padding: var(--space-2) var(--space-3);
    color: var(--text); cursor: pointer; font: inherit; text-align: left;
  }
  .node:hover { border-color: var(--border2); }
  .node.sel { border-color: var(--atomizer); background: color-mix(in oklab, var(--atomizer) 12%, var(--surface)); }
  .node.kid { font-size: 11px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text3); flex: none; }
  .dot.on { background: var(--ok); }
  .nlabel { flex: 1; font-size: 12px; font-family: var(--mono); }
  .proj { font-size: 9px; color: var(--text3); font-family: var(--mono); text-transform: uppercase; }

  .convo { display: none; flex-direction: column; flex: 1; min-height: 0; padding: var(--space-4); gap: var(--space-3); }
  .chead { display: flex; align-items: center; gap: var(--space-2); }
  .back { background: none; border: none; color: var(--atomizer); font: inherit; font-family: var(--mono); font-size: 12px; cursor: pointer; padding: 0; }
  .cscope { font-family: var(--mono); font-size: 12px; }
  .muted { color: var(--text3); }
  .stream { flex: 1; min-height: 200px; }
  .composer { flex: none; }

  /* mobile: one pane at a time */
  .agents.show-convo .roster { display: none; }
  .agents.show-convo .convo { display: flex; }

  /* desktop: two panes, no back button */
  @media (min-width: 720px) {
    .agents, .agents.show-convo { flex-direction: row; }
    .roster { width: 260px; flex: none; border-right: 1px solid var(--border); display: flex; }
    .convo, .agents.show-convo .convo { display: flex; }
    .agents.show-convo .roster { display: flex; }
    .back { display: none; }
  }
</style>
