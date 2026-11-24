<script lang="ts">
  import { Tag } from '@awm/primitives';
  import * as api from './api';
  import Badges from './Badges.svelte';
  import Dag from './Dag.svelte';
  import { STAGE_LABEL } from './tree';

  interface Props {
    ctx: api.Ctx;
    graph: api.Graph;
    id: string;
    /** The node's full read, once it lands. */
    detail: api.NodeDetail | null;
    tab: string;
    onselect: (id: string) => void;
    onref: (commit: string) => void;
    ontab: (tab: string) => void;
  }
  let { ctx, graph, id, detail, tab, onselect, onref, ontab }: Props = $props();

  const nodes = $derived(new Map(graph.nodes.map((n) => [n.id, n])));
  const node = $derived(nodes.get(id));

  /** The last drawing stays up while the next one is read, so picking through the DAG does not flash. */
  let geo = $state.raw<api.Layout | null>(null);
  let geoError = $state<string | null>(null);
  let commits = $state.raw<api.Commit[] | null>(null);
  let historyError = $state<string | null>(null);

  const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

  $effect(() => {
    if (!node) {
      geo = null;
      return;
    }
    let live = true;
    api
      .layout(ctx, id)
      .then((g) => {
        if (!live) return;
        geo = g;
        geoError = null;
      })
      .catch((e) => {
        if (!live) return;
        geo = null;
        geoError = message(e);
      });
    return () => {
      live = false;
    };
  });

  $effect(() => {
    commits = null;
    historyError = null;
    if (!node || tab !== 'history') return;
    let live = true;
    api
      .history(ctx, id)
      .then((c) => {
        if (live) commits = c;
      })
      .catch((e) => {
        if (live) historyError = message(e);
      });
    return () => {
      live = false;
    };
  });

  const hover = (ref: string) => {
    const n = nodes.get(ref);
    return n ? `${n.title}\n${ref}` : ref;
  };
  const sources = (s: api.GraphNode['source']) => (Array.isArray(s) ? s : s ? s.split(',') : []);
  const count = (n: number | undefined, unit: string) => n != null && `${n.toLocaleString()} ${unit}`;
  const summary = (p: api.Preview) =>
    [p.file, p.format, count(p.rows, 'rows'), count(p.bytes, 'bytes'), p.head && `first ${p.head.length} shown`]
      .filter(Boolean)
      .join(' · ');
</script>

{#if node}
  <div class="dag">
    {#if geo}
      <Dag {geo} title={hover} onpick={onselect} />
    {:else}
      <p class="meta" class:error={geoError}>{geoError ?? 'laying out…'}</p>
    {/if}
  </div>

  <div class="tabs">
    {#each ['provenance', 'history'] as t (t)}
      <button class:on={(tab === 'history') === (t === 'history')} onclick={() => ontab(t)}>{t}</button>
    {/each}
  </div>

  {#if tab === 'history'}
    {#if historyError}
      <p class="meta error">{historyError}</p>
    {:else if !commits}
      <p class="meta">loading…</p>
    {:else}
      <p class="meta">
        {node.stage === 'table' ? "Commits that moved this dataset's pin" : 'Commits that touched this node'}, from the worktree's HEAD back.
        Pick one to read the scope at that version.
      </p>
      <ul class="commits">
        {#each commits as c (c.commit)}
          <li>
            <button class:on={graph.commit === c.commit} onclick={() => onref(c.commit)} title={`${c.commit}\n${c.author}`}>
              <span class="line"><code>{c.short}</code><span class="when">{c.date.slice(0, 16).replace('T', ' ')}</span></span>
              <span class="subject">{c.subject}</span>
            </button>
          </li>
        {:else}
          <li class="meta">No commit touched it yet.</li>
        {/each}
      </ul>
    {/if}
  {:else}
    <div class="prov" data-stage={node.stage}>
      <div class="kind">
        <span class="stage">{STAGE_LABEL[node.stage]}</span>
        {#if node.status}<Tag tone={node.status === 'accepted' ? 'ok' : 'neutral'}>{node.status}</Tag>{/if}
        <Badges {node} />
      </div>
      <p class="meta">{node.id}</p>

      {#if node.stale_inputs?.length}
        <h2>Stale inputs</h2>
        <ul class="plain">
          {#each node.stale_inputs as ref (ref)}
            <li><button class="ref" onclick={() => onselect(ref)} title={ref}>{nodes.get(ref)?.title ?? ref}</button></li>
          {/each}
        </ul>
      {/if}
      {#if node.stale_ok}
        <h2>Stale accepted</h2>
        <p class="note">{typeof node.stale_ok === 'string' ? node.stale_ok : JSON.stringify(node.stale_ok)}</p>
      {/if}
      {#if node.review}
        <h2>Reviewed {node.review.on}</h2>
        <p class="note">{node.review.note}</p>
      {/if}

      {#if node.stage === 'table'}
        {#if node.kind === 'product'}<p class="meta">{node.files} files · {node.samples} samples</p>{/if}
        {#if detail?.preview}<p class="meta">{summary(detail.preview)}</p>{/if}
        {#if node.gaps?.length}
          <h2>Gaps</h2>
          <ul class="plain">{#each node.gaps as g}<li>{Object.entries(g).map(([k, v]) => `${k}: ${v}`).join(' · ')}</li>{/each}</ul>
        {/if}
        {#if sources(node.source).length}
          <h2>Source</h2>
          <ul class="plain">{#each sources(node.source) as s (s)}<li>{s}</li>{/each}</ul>
        {/if}
        {#if detail?.preview?.columns}
          <details>
            <summary>Columns ({detail.preview.columns.length})</summary>
            <ul class="plain cols">
              {#each detail.preview.columns as c (c.name)}<li>{c.name}{#if c.type}<em>{c.type}</em>{/if}</li>{/each}
            </ul>
          </details>
        {/if}
        {#if detail?.listing}
          <details>
            <summary>Files ({detail.listing.length}{detail.listing.length < (node.files ?? 0) ? ` of ${node.files}` : ''})</summary>
            <ul class="plain">{#each detail.listing as f (f.file)}<li>{f.file}</li>{/each}</ul>
          </details>
        {/if}
      {:else}
        {#if node.artifact}
          <h2>Artifact</h2>
          <p class="meta">{node.artifact}</p>
        {/if}
        {#if node.made_by}
          <h2>Made by</h2>
          {#if detail?.made_by_source}
            <details>
              <summary>{node.made_by}</summary>
              <pre>{detail.made_by_source}</pre>
            </details>
          {:else}
            <p class="meta">{node.made_by}</p>
          {/if}
        {/if}
      {/if}
    </div>
  {/if}
{/if}

<style>
  .dag { overflow: auto; max-height: 55vh; padding: var(--space-3); border-bottom: 1px solid var(--border); }
  .tabs { display: flex; gap: var(--space-5); padding: 0 var(--space-4); border-bottom: 1px solid var(--border); }
  .tabs button { all: unset; cursor: pointer; padding: var(--space-2) 0; font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--text2); border-bottom: 2px solid transparent; }
  .tabs button.on { color: var(--text); border-color: var(--atomizer); }
  .tabs button:focus-visible { outline: 1px solid var(--atomizer); }

  .prov, .commits, .tabs ~ .meta { margin: 0; padding: var(--space-3) var(--space-4); }
  .prov { border-left: 2px solid var(--stage); }
  .kind { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-3); }
  .stage { font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--stage); }
  h2 { font-family: var(--mono); font-size: 10px; font-weight: 400; letter-spacing: 1px; text-transform: uppercase; color: var(--text3); margin: var(--space-5) 0 var(--space-1); }
  .meta { font-family: var(--mono); font-size: 11px; color: var(--text2); margin: var(--space-2) 0; overflow-wrap: anywhere; }
  .error { color: var(--danger); }
  .note { margin: 0; color: var(--text2); line-height: 1.5; }
  .ref { all: unset; cursor: pointer; color: var(--text); text-decoration: underline; }
  .ref:focus-visible { outline: 1px solid var(--atomizer); }

  details { margin: var(--space-4) 0 0; }
  summary { cursor: pointer; color: var(--text2); font-family: var(--mono); font-size: 11px; overflow-wrap: anywhere; }
  .plain { list-style: none; padding: 0; margin: var(--space-1) 0 0; font-family: var(--mono); font-size: 11px; color: var(--text2); overflow-wrap: anywhere; }
  .plain li { padding: var(--space-0) 0; }
  .cols em { font-style: normal; color: var(--text3); margin-left: var(--space-3); }
  pre { margin: var(--space-2) 0 0; padding: var(--space-3); overflow: auto; max-height: 50vh; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-lg); font-family: var(--mono); font-size: 11px; }

  .commits { list-style: none; padding-top: 0; }
  .commits button { all: unset; cursor: pointer; display: flex; flex-direction: column; gap: var(--space-0); width: 100%; padding: var(--space-2) var(--space-3); box-sizing: border-box; border-bottom: 1px solid var(--border); }
  .commits button:hover, .commits button.on { background: var(--surface2); }
  .commits button.on { box-shadow: inset 2px 0 0 var(--atomizer); }
  .commits button:focus-visible { outline: 1px solid var(--atomizer); }
  .line { display: flex; gap: var(--space-4); align-items: baseline; }
  .commits code { color: var(--atomizer); font-family: var(--mono); font-size: 11px; }
  .when { color: var(--text3); font-family: var(--mono); font-size: 11px; white-space: nowrap; }
  .subject { overflow-wrap: anywhere; }
</style>
