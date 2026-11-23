<script lang="ts">
  import { Tag } from '@awm/primitives';
  import * as api from './api';
  import Badges from './Badges.svelte';
  import Figure from './Figure.svelte';
  import { renderMarkdown } from './markdown';
  import { STAGE_LABEL, lineage } from './tree';

  interface Props {
    ctx: api.Ctx;
    graph: api.Graph;
    id: string;
    tab: string;
    onselect: (id: string) => void;
    onref: (commit: string) => void;
    ontab: (tab: string) => void;
  }
  let { ctx, graph, id, tab, onselect, onref, ontab }: Props = $props();

  const nodes = $derived(new Map(graph.nodes.map((n) => [n.id, n])));
  const node = $derived(nodes.get(id));
  const trail = $derived(lineage(graph, id));

  let detail = $state.raw<api.NodeDetail | null>(null);
  let commits = $state.raw<api.Commit[] | null>(null);
  let error = $state<string | null>(null);

  $effect(() => {
    detail = null;
    commits = null;
    error = null;
    if (!node) return;
    let live = true;
    const read =
      tab === 'history'
        ? api.history(ctx, id).then((c) => {
            if (live) commits = c;
          })
        : api.node(ctx, id).then((d) => {
            if (live) detail = d;
          });
    read.catch((e) => {
      if (live) error = e instanceof Error ? e.message : String(e);
    });
    return () => {
      live = false;
    };
  });

  const sources = (s: api.GraphNode['source']) => (Array.isArray(s) ? s : s ? s.split(',') : []);
  const summary = (p: api.Preview) =>
    [
      p.file,
      p.format,
      p.rows != null && `${p.rows.toLocaleString()} rows`,
      p.bytes != null && `${p.bytes.toLocaleString()} bytes`,
      p.head && `first ${p.head.length} shown`,
    ]
      .filter(Boolean)
      .join(' · ');
  const cell = (v: unknown) => (v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v));
</script>

{#snippet chip(ref: string, current = false)}
  {@const n = nodes.get(ref)}
  {#if n}
    <button class="chip" class:current data-stage={n.stage} onclick={() => onselect(ref)} title={ref}>
      <i></i>{n.title}
    </button>
  {:else}
    <span class="chip missing" title="names no node at this ref">{ref}</span>
  {/if}
{/snippet}

{#snippet chips(label: string, refs: string[])}
  {#if refs.length}
    <div class="chips"><span class="k">{label}</span>{#each refs as ref (ref)}{@render chip(ref)}{/each}</div>
  {/if}
{/snippet}

{#snippet preview(p: api.Preview)}
  <p class="meta">{summary(p)}</p>
  {#if p.columns}
    <div class="cols">
      {#each p.columns as c (c.name)}<span>{c.name}{#if c.type}<em>{c.type}</em>{/if}</span>{/each}
    </div>
    <div class="grid">
      <table>
        <thead><tr>{#each p.columns as c (c.name)}<th>{c.name}</th>{/each}</tr></thead>
        <tbody>
          {#each p.head ?? [] as r}
            <tr>{#each p.columns as c (c.name)}<td class:null={r[c.name] == null}>{cell(r[c.name])}</td>{/each}</tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <p class="meta">No preview for this format.</p>
  {/if}
{/snippet}

{#if !node}
  <p class="meta">{id ? `No node ${id} at this ref.` : 'Pick a node from the tree.'}</p>
{:else}
  <header data-stage={node.stage}>
    <div class="kind">
      <span class="stage">{STAGE_LABEL[node.stage]}</span>
      {#if node.status}<Tag tone={node.status === 'accepted' ? 'ok' : 'neutral'}>{node.status}</Tag>{/if}
      <Badges {node} />
    </div>
    <h1>{node.title}</h1>
    <p class="meta">{node.id} · {node.path}</p>
  </header>

  <div class="trail">
    {#each trail as step, i (step.stage)}
      {#if i}<span class="arrow">→</span>{/if}
      <div class="step">
        <span class="k">{STAGE_LABEL[step.stage]}</span>
        {#each step.nodes as n (n.id)}{@render chip(n.id, n.id === id)}{/each}
      </div>
    {/each}
  </div>

  {@render chips('Built from', node.from ?? [])}
  {@render chips('Used by', node.used_by)}
  {@render chips('Stale inputs', node.stale_inputs ?? [])}
  {#if node.review}
    <p class="review"><span class="k">Reviewed {node.review.on}</span>{node.review.note}</p>
  {/if}
  {#if node.stale_ok}
    <p class="review"><span class="k">Stale accepted</span>{typeof node.stale_ok === 'string' ? node.stale_ok : ''}</p>
  {/if}

  <div class="tabs">
    {#each ['view', 'history'] as t (t)}
      <button class:on={tab === t} onclick={() => ontab(t)}>{t}</button>
    {/each}
  </div>

  {#if error}
    <p class="error">{error}</p>
  {:else if tab === 'history'}
    {#if !commits}
      <p class="meta">loading…</p>
    {:else}
      <p class="meta">
        {node.stage === 'table' ? "Commits that moved this dataset's pin" : 'Commits that touched this node'}, from the worktree's HEAD back.
        Pick one to read the scope at that version.
      </p>
      <ul class="commits">
        {#each commits as c (c.commit)}
          <li>
            <button class:on={graph.commit === c.commit} onclick={() => onref(c.commit)}>
              <code>{c.short}</code><span class="when">{c.date.slice(0, 16).replace('T', ' ')}</span>
              <span class="subject">{c.subject}</span><span class="when">{c.author}</span>
            </button>
          </li>
        {:else}
          <li class="meta">No commit touched it yet.</li>
        {/each}
      </ul>
    {/if}
  {:else if !detail}
    <p class="meta">loading…</p>
  {:else if detail.stage === 'table'}
    {#if detail.kind === 'product'}
      <p class="meta">{detail.files} files · {detail.samples} samples</p>
      {#if detail.gaps?.length}
        <h2>Gaps</h2>
        <ul class="plain">{#each detail.gaps as g}<li>{Object.entries(g).map(([k, v]) => `${k}: ${v}`).join(' · ')}</li>{/each}</ul>
      {/if}
    {/if}
    {#if detail.preview}{@render preview(detail.preview)}{/if}
    {#if detail.listing}
      <details open={!detail.preview}>
        <summary>Files ({detail.listing.length}{detail.listing.length < (detail.files ?? 0) ? ` of ${detail.files}` : ''})</summary>
        <ul class="plain">{#each detail.listing as f (f.file)}<li>{f.file}</li>{/each}</ul>
      </details>
    {/if}
    {#if sources(detail.source).length}
      <details>
        <summary>Source ({sources(detail.source).length})</summary>
        <ul class="plain">{#each sources(detail.source) as s (s)}<li>{s}</li>{/each}</ul>
      </details>
    {/if}
  {:else}
    {#if detail.artifact_error}<p class="error">{detail.artifact_error}</p>{/if}
    {#if detail.figure}<Figure figure={detail.figure} />{/if}
    <article>{@html renderMarkdown(detail.body ?? '')}</article>
    {#if detail.made_by_source}
      <details>
        <summary>Made by {detail.made_by}</summary>
        <pre>{detail.made_by_source}</pre>
      </details>
    {/if}
  {/if}
{/if}

<style>
  header { border-left: 2px solid var(--stage); padding-left: var(--space-4); }
  .kind { display: flex; align-items: center; gap: var(--space-3); }
  .stage, .k { font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; }
  .stage { color: var(--stage); }
  .k { color: var(--text3); margin-right: var(--space-2); }
  h1 { font-size: 18px; font-weight: 500; margin: var(--space-2) 0; }
  h2 { font-size: 13px; font-weight: 500; margin: var(--space-5) 0 var(--space-2); }
  .meta { font-family: var(--mono); font-size: 11px; color: var(--text2); margin: var(--space-2) 0; overflow-wrap: anywhere; }
  .error { color: var(--danger); }

  .trail { display: flex; align-items: flex-start; gap: var(--space-3); overflow-x: auto; margin: var(--space-5) 0; padding: var(--space-3); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); }
  .step { display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-1); min-width: 120px; max-width: 220px; }
  .arrow { color: var(--text3); align-self: center; }
  .chips { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-1); margin: var(--space-2) 0; }
  .chip { all: unset; display: inline-flex; align-items: center; gap: var(--space-2); max-width: 100%; padding: var(--space-0) var(--space-3); border: 1px solid var(--border2); border-radius: 10px; font-size: 11px; cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; box-sizing: border-box; }
  .chip:hover { background: var(--surface2); }
  .chip:focus-visible { outline: 1px solid var(--atomizer); }
  .chip i { width: 6px; height: 6px; border-radius: 50%; background: var(--stage); flex: none; }
  .chip.current { border-color: var(--stage); background: var(--surface3); }
  .chip.missing { cursor: default; color: var(--danger); border-style: dashed; }
  .review { margin: var(--space-2) 0; color: var(--text2); }

  .tabs { display: flex; gap: var(--space-5); border-bottom: 1px solid var(--border); margin: var(--space-5) 0 var(--space-4); }
  .tabs button { all: unset; cursor: pointer; padding: var(--space-2) 0; font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--text2); border-bottom: 2px solid transparent; }
  .tabs button.on { color: var(--text); border-color: var(--atomizer); }

  .cols { display: flex; flex-wrap: wrap; gap: var(--space-1); margin-bottom: var(--space-3); }
  .cols span { font-family: var(--mono); font-size: 11px; padding: var(--space-0) var(--space-2); background: var(--surface2); border-radius: var(--radius-md); }
  .cols em { font-style: normal; color: var(--text3); margin-left: var(--space-2); }
  .grid { overflow: auto; max-height: 60vh; border: 1px solid var(--border); border-radius: var(--radius-lg); }
  table { border-collapse: collapse; font-family: var(--mono); font-size: 11px; }
  th, td { padding: var(--space-1) var(--space-3); text-align: left; white-space: nowrap; max-width: 320px; overflow: hidden; text-overflow: ellipsis; border-bottom: 1px solid var(--border); }
  th { position: sticky; top: 0; background: var(--surface2); color: var(--text2); font-weight: 500; }
  td.null::after { content: '∅'; color: var(--text3); }

  details { margin: var(--space-4) 0; }
  summary { cursor: pointer; color: var(--text2); font-family: var(--mono); font-size: 11px; }
  .plain { list-style: none; padding: 0; margin: var(--space-2) 0; font-family: var(--mono); font-size: 11px; color: var(--text2); overflow-wrap: anywhere; }
  .plain li { padding: var(--space-0) 0; }
  pre { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: var(--space-4); overflow: auto; font-family: var(--mono); font-size: 11px; }

  .commits { list-style: none; padding: 0; margin: 0; }
  .commits button { all: unset; cursor: pointer; display: flex; gap: var(--space-4); align-items: baseline; width: 100%; padding: var(--space-2) var(--space-3); box-sizing: border-box; border-bottom: 1px solid var(--border); }
  .commits button:hover, .commits button.on { background: var(--surface2); }
  .commits code { color: var(--atomizer); font-family: var(--mono); }
  .when { color: var(--text3); font-family: var(--mono); font-size: 11px; white-space: nowrap; }
  .subject { flex: 1; }

  article { max-width: 72ch; line-height: 1.6; font-size: 14px; margin-top: var(--space-4); }
  article :global(code), article :global(pre) { font-family: var(--mono); font-size: 12px; background: var(--surface2); border-radius: var(--radius-md); }
  article :global(code) { padding: 0 var(--space-1); }
  article :global(pre) { padding: var(--space-4); overflow: auto; }
  article :global(a) { color: var(--atomizer); }
  article :global(h3), article :global(h4) { font-weight: 500; }
</style>
