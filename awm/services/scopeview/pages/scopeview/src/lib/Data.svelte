<script lang="ts">
  import * as api from './api';
  import Figure from './Figure.svelte';
  import { colourJson } from './json';
  import { renderMarkdown } from './markdown';

  interface Props {
    ctx: api.Ctx;
    /** The commit the scope was read at. */
    commit: string;
    node: api.GraphNode;
    detail: api.NodeDetail | null;
    error: string | null;
  }
  let { ctx, commit, node, detail, error }: Props = $props();

  /** One line that names this node to a reader outside the page. */
  const citation = $derived(
    `${ctx.project}/${ctx.scope}@${ctx.ref ? commit.slice(0, 10) : 'worktree'} ${node.path} — ${node.title}`,
  );
  let copied = $state(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(citation);
      copied = true;
      setTimeout(() => (copied = false), 1200);
    } catch {
      // the header text is selectable, so a refused clipboard still leaves a way to copy
    }
  }

  const cell = (v: unknown) => (v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v));
</script>

<header data-stage={node.stage}>
  <code>{node.path}</code>
  <span class="title" title={node.title}>{node.title}</span>
  <button onclick={copy} title={citation}>{copied ? 'copied' : 'copy'}</button>
</header>

<div class="content">
  {#if error}
    <p class="error">{error}</p>
  {:else if !detail}
    <p class="dim">loading…</p>
  {:else if detail.stage === 'table'}
    {@const p = detail.preview}
    {#if p?.columns}
      <table>
        <thead><tr>{#each p.columns as c (c.name)}<th>{c.name}</th>{/each}</tr></thead>
        <tbody>
          {#each p.head ?? [] as r}
            <tr>{#each p.columns as c (c.name)}<td class:null={r[c.name] == null}>{cell(r[c.name])}</td>{/each}</tr>
          {/each}
        </tbody>
      </table>
    {:else if p?.text != null}
      {#if p.format === 'json'}
        <pre class="json">{@html colourJson(p.text)}</pre>
      {:else}
        <pre>{p.text}</pre>
      {/if}
    {:else if p}
      <p class="dim">{p.format} · {p.bytes?.toLocaleString() ?? '?'} bytes · too large or not text, so not shown</p>
    {:else}
      <p class="dim">No file at this path to show.</p>
    {/if}
  {:else}
    {#if detail.artifact_error}<p class="error">{detail.artifact_error}</p>{/if}
    {#if detail.figure}{#key detail.id}<Figure figure={detail.figure} />{/key}{/if}
    <article>{@html renderMarkdown(detail.body ?? '')}</article>
  {/if}
</div>

<style>
  header { display: flex; align-items: baseline; gap: var(--space-4); flex: none; padding: var(--space-3) var(--space-5); border-bottom: 1px solid var(--border); box-shadow: inset 2px 0 0 var(--stage); }
  header code { font-family: var(--mono); font-size: 11px; color: var(--text2); flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .title { flex: 1 1 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
  header button { all: unset; cursor: pointer; flex: none; padding: 0 var(--space-3); border: 1px solid var(--border2); border-radius: var(--radius-md); font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--text2); }
  header button:hover { color: var(--text); background: var(--surface2); }
  header button:focus-visible { outline: 1px solid var(--atomizer); }

  .content { flex: 1; min-height: 0; overflow: auto; padding: var(--space-5); }
  .content:has(> table) { padding: 0; }
  .dim { color: var(--text2); margin: 0; }
  .error { color: var(--danger); margin: 0 0 var(--space-3); }

  table { border-collapse: collapse; font-family: var(--mono); font-size: 11px; }
  th, td { padding: var(--space-1) var(--space-3); text-align: left; white-space: nowrap; max-width: 320px; overflow: hidden; text-overflow: ellipsis; border-bottom: 1px solid var(--border); }
  th { position: sticky; top: 0; background: var(--surface2); color: var(--text2); font-weight: 500; }
  tbody tr:hover { background: var(--surface); }
  td.null::after { content: '∅'; color: var(--text3); }

  pre { margin: 0; font-family: var(--mono); font-size: 12px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
  .json :global(.key) { color: var(--json-key); }
  .json :global(.string) { color: var(--json-string); }
  .json :global(.number) { color: var(--json-number); }
  .json :global(.atom) { color: var(--json-atom); }

  article { max-width: 72ch; line-height: 1.6; font-size: 14px; }
  article :global(:first-child) { margin-top: 0; }
  article :global(p), article :global(ul), article :global(ol) { margin: 0 0 var(--space-4); }
  article :global(ul), article :global(ol) { padding-left: var(--space-6); }
  article :global(h3), article :global(h4), article :global(h5), article :global(h6) { font-weight: 500; margin: var(--space-6) 0 var(--space-3); }
  article :global(code), article :global(pre) { font-family: var(--mono); font-size: 12px; background: var(--surface2); border-radius: var(--radius-md); }
  article :global(code) { padding: 0 var(--space-1); }
  article :global(pre) { padding: var(--space-4); overflow: auto; white-space: pre; }
  article :global(a) { color: var(--atomizer); }
</style>
