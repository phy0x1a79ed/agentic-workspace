<script lang="ts">
  import { Input, Select, Tag } from '@awm/primitives';
  import * as api from './lib/api';
  import Chat from './lib/Chat.svelte';
  import Data from './lib/Data.svelte';
  import Info from './lib/Info.svelte';
  import Tree from './lib/Tree.svelte';
  import { theme, toggleTheme } from './lib/theme.svelte';
  import type { Target } from './lib/trace';
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
  let tab = $state(h.get('tab') ?? '');
  let tracing = $state(h.has('trace'));
  let chatting = $state(h.has('chat'));

  let peerDraft = $state(startPeer);
  let scopes = $state.raw<api.ScopeInfo[]>([]);
  let versions = $state.raw<api.Commit[]>([]);
  /** The graph with the context it was read in, so no panel reads a node across two refs. */
  let view = $state.raw<{ ctx: api.Ctx; graph: api.Graph } | null>(null);
  let detail = $state.raw<api.NodeDetail | null>(null);
  let detailError = $state<string | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let reads = $state(0);
  let filter = $state('');
  let showProblems = $state(false);
  let treeWidth = $state(300);
  let infoWidth = $state(340);
  let chatWidth = $state(340);

  const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

  $effect(() => {
    const state = { node: peer, project, scope, ref, id, tab: tab === 'history' ? tab : '', trace: tracing ? '1' : '', chat: chatting ? '1' : '' };
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
    tab = p.get('tab') ?? '';
    tracing = p.has('trace');
    chatting = p.has('chat');
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

  const selected = $derived(view?.graph.nodes.find((n) => n.id === id));

  $effect(() => {
    detail = null;
    detailError = null;
    if (!view || !selected) return;
    let live = true;
    api
      .node(view.ctx, selected.id)
      .then((d) => {
        if (live) detail = d;
      })
      .catch((e) => {
        if (live) detailError = message(e);
      });
    return () => {
      live = false;
    };
  });

  /** What the data panel points at: the pointer's target, or the clicked one, which holds until released. */
  let hovered = $state.raw<Target | null>(null);
  let pin = $state.raw<Target | null>(null);
  let traced = $state.raw<{ target: Target; trace?: api.Trace; error?: string } | null>(null);
  const target = $derived(tracing ? (pin ?? hovered) : null);
  /** Traces already asked for at this read of the graph, by their citations. */
  const traces = new Map<string, Promise<api.Trace>>();

  $effect(() => {
    void view;
    traces.clear();
  });
  $effect(() => {
    void view;
    void id;
    hovered = pin = null;
  });

  $effect(() => {
    const t = target;
    const ctx = view?.ctx;
    if (!t || !ctx) {
      traced = null;
      return;
    }
    if (t.local) {
      traced = { target: t, trace: t.local };
      return;
    }
    const cites = t.cites ?? [];
    const key = cites.join(' ');
    let live = true;
    const show = (p: Promise<api.Trace>) =>
      p.then(
        (trace) => live && (traced = { target: t, trace }),
        (e) => live && (traced = { target: t, error: message(e) }),
      );
    const known = traces.get(key);
    // A hover waits before it asks, so sweeping the pointer across a paragraph reads nothing.
    const timer = known
      ? (show(known), undefined)
      : setTimeout(() => {
          const p = api.trace(ctx, cites);
          traces.set(key, p);
          p.catch(() => traces.delete(key));
          show(p);
        }, 150);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  });

  const chatContext = $derived(
    Object.fromEntries(Object.entries({ ref, node: id, cite: pin?.cites?.join(' ') ?? id }).filter(([, v]) => v)),
  );

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

  /** Drags a column edge: `set` takes the width, which grows with the pointer's travel times `toward`. */
  function drag(e: PointerEvent, start: number, set: (width: number) => void, toward = 1) {
    const handle = e.currentTarget as HTMLElement;
    handle.setPointerCapture(e.pointerId);
    const move = (m: PointerEvent) => set(Math.min(Math.max(start + toward * (m.clientX - e.clientX), 180), 720));
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', () => handle.removeEventListener('pointermove', move), { once: true });
  }
</script>

<svelte:window {onhashchange} onkeydown={(e) => e.key === 'Escape' && (pin = null)} />

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
    <button class="plain theme" class:on={chatting} aria-pressed={chatting} onclick={() => (chatting = !chatting)} title="Talk to this study's curator">
      chat
    </button>
    <button class="plain theme" onclick={toggleTheme} title="Switch to the {theme.current === 'dark' ? 'light' : 'dark'} theme">
      {theme.current === 'dark' ? '☀ light' : '☾ dark'}
    </button>
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

  <main style:grid-template-columns="{treeWidth}px 5px {infoWidth}px 5px minmax(0, 1fr){chatting ? ` 5px ${chatWidth}px` : ''}">
    <aside>
      <div class="filter"><Input bind:value={filter} type="search" placeholder="filter nodes" /></div>
      <Tree items={tree} selected={id} onselect={(next) => (id = next)} />
    </aside>
    <div class="handle" onpointerdown={(e) => drag(e, treeWidth, (w) => (treeWidth = w))} role="separator" aria-orientation="vertical"></div>
    {#if view && selected}
      <aside>
        <Info
          ctx={view.ctx}
          graph={view.graph}
          {id}
          {detail}
          {tab}
          {tracing}
          {target}
          pinned={!!pin}
          {traced}
          onselect={(next) => (id = next)}
          onref={(commit) => (ref = commit)}
          ontab={(next) => (tab = next)}
        />
      </aside>
      <div class="handle" onpointerdown={(e) => drag(e, infoWidth, (w) => (infoWidth = w))} role="separator" aria-orientation="vertical"></div>
      <section>
        <Data
          ctx={view.ctx}
          commit={view.graph.commit}
          node={selected}
          {detail}
          error={detailError}
          {tracing}
          pinned={pin?.key ?? null}
          ontrace={(t, hold) => (hold ? (pin = t) : (hovered = t))}
          ontoggle={() => (tracing = !tracing)}
          onselect={(next) => (id = next)}
        />
      </section>
    {:else}
      <p class="dim">
        {#if view}
          {id ? `No node ${id} at this ref.` : 'Pick a node from the tree.'}
        {:else if loading}
          reading the scope…
        {:else if !error}
          Pick a node and a scope.
        {/if}
      </p>
    {/if}
    {#if chatting && project && scope}
      <div class="handle" onpointerdown={(e) => drag(e, chatWidth, (w) => (chatWidth = w), -1)} role="separator" aria-orientation="vertical"></div>
      <aside><Chat {peer} {project} {scope} context={chatContext} onhost={() => reads++} /></aside>
    {/if}
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
  aside, section { min-height: 0; min-width: 0; }
  aside { overflow: auto; background: var(--surface); }
  section { display: flex; flex-direction: column; }
  .filter { position: sticky; top: 0; z-index: 1; padding: var(--space-3); background: var(--surface); border-bottom: 1px solid var(--border); }
  .handle { cursor: col-resize; background: var(--border); touch-action: none; }
  .handle:hover { background: var(--border2); }
  .dim { grid-column: 3 / 6; margin: 0; padding: var(--space-6); color: var(--text2); }
  .theme { font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; padding: var(--space-0) var(--space-3); border: 1px solid var(--border2); border-radius: var(--radius-md); }
  .theme:hover { color: var(--text); background: var(--surface2); }
  .theme.on { color: var(--bg); background: var(--atomizer); border-color: var(--atomizer); }
</style>
