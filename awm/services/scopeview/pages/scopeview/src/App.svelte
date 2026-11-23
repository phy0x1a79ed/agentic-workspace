<script lang="ts">
  import { Input, Select, Tag } from '@awm/primitives';
  import * as api from './lib/api';
  import Tree from './lib/Tree.svelte';
  import Viewer from './lib/Viewer.svelte';
  import { buildTree } from './lib/tree';

  const WORKTREE = 'worktree';

  const fromHash = () => new URLSearchParams(location.hash.slice(1));
  const h = fromHash();
  const startPeer = h.get('node') ?? 'altair';
  let peer = $state(startPeer);
  let project = $state(h.get('project') ?? '');
  let scope = $state(h.get('scope') ?? '');
  let ref = $state(h.get('ref') ?? '');
  let id = $state(h.get('id') ?? '');
  let tab = $state(h.get('tab') ?? 'view');

  let peerDraft = $state(startPeer);
  let scopes = $state.raw<api.ScopeInfo[]>([]);
  let versions = $state.raw<api.Commit[]>([]);
  /** The graph with the context it was read in, so the viewer never reads a node across two refs. */
  let view = $state.raw<{ ctx: api.Ctx; graph: api.Graph } | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let reads = $state(0);
  let filter = $state('');
  let showProblems = $state(false);
  let split = $state(340);

  const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

  $effect(() => {
    const state = { node: peer, project, scope, ref, id, tab: tab === 'view' ? '' : tab };
    const next = new URLSearchParams(Object.entries(state).filter(([, v]) => v));
    history.replaceState(null, '', `#${next}`);
  });

  function onhashchange() {
    const p = fromHash();
    peer = peerDraft = p.get('node') ?? 'altair';
    project = p.get('project') ?? '';
    scope = p.get('scope') ?? '';
    ref = p.get('ref') ?? '';
    id = p.get('id') ?? '';
    tab = p.get('tab') ?? 'view';
  }

  $effect(() => {
    let live = true;
    scopes = [];
    api
      .scopes(peer)
      .then((list) => {
        if (!live) return;
        scopes = list;
        if (!list.some((s) => s.project === project && s.scope === scope) && list[0]) pickScope(list[0]);
      })
      .catch((e) => {
        if (live) error = message(e);
      });
    return () => {
      live = false;
    };
  });

  $effect(() => {
    versions = [];
    if (!project || !scope) return;
    let live = true;
    api
      .history({ peer, project, scope, ref: '' })
      .then((c) => {
        if (live) versions = c;
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  });

  $effect(() => {
    void reads;
    if (!project || !scope) return;
    const ctx = { peer, project, scope, ref };
    let live = true;
    loading = true;
    api
      .graph(ctx)
      .then((graph) => {
        if (!live) return;
        view = { ctx, graph };
        error = null;
      })
      .catch((e) => {
        if (!live) return;
        view = null;
        error = message(e);
      })
      .finally(() => {
        if (live) loading = false;
      });
    return () => {
      live = false;
    };
  });

  function pickScope(s: api.ScopeInfo) {
    project = s.project;
    scope = s.scope;
    ref = '';
    id = '';
  }

  function setPeer() {
    const next = peerDraft.trim() || 'local';
    if (next === peer) return;
    peer = peerDraft = next;
    view = null;
    project = scope = ref = id = '';
  }

  const scopeLabel = (s: { project: string; scope: string }) => `${s.project}/${s.scope}`;
  const versionLabel = (c: api.Commit) => `${c.short} ${c.date.slice(0, 10)} ${c.subject}`;
  const refOptions = $derived.by(() => {
    const listed = new Map(versions.map((c) => [versionLabel(c), c.commit]));
    const current = [...listed].find(([, commit]) => commit === ref)?.[0] ?? (ref ? ref.slice(0, 7) : WORKTREE);
    return new Map([[WORKTREE, ''], [current, ref], ...listed]);
  });
  const refLabel = $derived([...refOptions].find(([, commit]) => commit === ref)?.[0] ?? WORKTREE);

  const graph = $derived(view?.graph);
  const tree = $derived(graph ? buildTree(graph, filter) : []);

  function drag(e: PointerEvent) {
    const handle = e.currentTarget as HTMLElement;
    handle.setPointerCapture(e.pointerId);
    const move = (m: PointerEvent) => (split = Math.min(Math.max(m.clientX, 200), window.innerWidth - 320));
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', () => handle.removeEventListener('pointermove', move), { once: true });
  }
</script>

<svelte:window {onhashchange} />

<div class="page">
  <header>
    <strong>scopeview</strong>
    <label class="peer">
      <span>node</span>
      <Input bind:value={peerDraft} placeholder="local" onblur={setPeer} onkeydown={(e) => e.key === 'Enter' && setPeer()} />
    </label>
    <label class="scope">
      <span>scope</span>
      <Select
        value={project ? scopeLabel({ project, scope }) : ''}
        options={scopes.map(scopeLabel)}
        placeholder={scopes.length ? 'pick a scope' : 'no scopes on this node'}
        onchange={(v) => pickScope(scopes.find((s) => scopeLabel(s) === v)!)}
      />
    </label>
    <label class="ref">
      <span>ref</span>
      <Select value={refLabel} options={[...refOptions.keys()]} onchange={(v) => (ref = refOptions.get(v) ?? '')} />
    </label>
    <button class="plain" onclick={() => reads++} title="Read the graph again">{loading ? '…' : '↻'}</button>
    <span class="spacer"></span>
    {#if graph}
      <code title={graph.commit}>{graph.commit.slice(0, 7)}</code>
      <button class="plain" onclick={() => (showProblems = !showProblems)} disabled={graph.ok}>
        <Tag tone={graph.ok ? 'ok' : 'danger'}>{graph.ok ? 'ok' : `${graph.problems.length} problems`}</Tag>
      </button>
    {/if}
  </header>

  {#if error}<p class="error">{error}</p>{/if}
  {#if graph && showProblems && !graph.ok}
    <ul class="problems">
      {#each graph.problems as p}
        <li>
          <button class="plain" onclick={() => (id = p.node)}>{p.node}</button>
          <code>{p.code}</code>
          {p.message}
        </li>
      {/each}
    </ul>
  {/if}

  <main style:grid-template-columns="{split}px 5px minmax(0, 1fr)">
    <aside>
      <div class="filter"><Input bind:value={filter} type="search" placeholder="filter nodes" /></div>
      <Tree items={tree} selected={id} onselect={(next) => (id = next)} />
    </aside>
    <div class="handle" onpointerdown={drag} role="separator" aria-orientation="vertical"></div>
    <section>
      {#if view}
        <Viewer
          ctx={view.ctx}
          graph={view.graph}
          {id}
          {tab}
          onselect={(next) => (id = next)}
          onref={(commit) => (ref = commit)}
          ontab={(next) => (tab = next)}
        />
      {:else if loading}
        <p class="dim">reading the scope…</p>
      {:else if !error}
        <p class="dim">Pick a node and a scope.</p>
      {/if}
    </section>
  </main>
</div>

<style>
  .page { display: flex; flex-direction: column; height: 100vh; }
  header { display: flex; align-items: center; gap: var(--space-5); padding: var(--space-3) var(--space-5); border-bottom: 1px solid var(--border); background: var(--surface); }
  label { display: flex; align-items: center; gap: var(--space-2); }
  label span { font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--text3); }
  .peer { width: 130px; }
  .scope { width: 300px; }
  .ref { width: 340px; min-width: 0; flex-shrink: 1; }
  .spacer { flex: 1; }
  code { font-family: var(--mono); font-size: 11px; color: var(--text2); }
  .plain { all: unset; cursor: pointer; color: var(--text2); }
  .plain:disabled { cursor: default; }
  .plain:focus-visible { outline: 1px solid var(--atomizer); }
  .error { color: var(--danger); margin: 0; padding: var(--space-3) var(--space-5); border-bottom: 1px solid var(--border); }
  .problems { list-style: none; margin: 0; padding: var(--space-3) var(--space-5); max-height: 30vh; overflow: auto; border-bottom: 1px solid var(--border); background: var(--surface); }
  .problems li { display: flex; gap: var(--space-4); padding: var(--space-0) 0; }
  .problems .plain { color: var(--text); text-decoration: underline; }
  .problems code { color: var(--danger); }

  main { display: grid; flex: 1; min-height: 0; }
  aside, section { overflow: auto; min-height: 0; }
  aside { background: var(--surface); }
  .filter { position: sticky; top: 0; z-index: 1; padding: var(--space-3); background: var(--surface); border-bottom: 1px solid var(--border); }
  .handle { cursor: col-resize; background: var(--border); touch-action: none; }
  .handle:hover { background: var(--border2); }
  section { padding: var(--space-6); }
  .dim { color: var(--text2); }
</style>
