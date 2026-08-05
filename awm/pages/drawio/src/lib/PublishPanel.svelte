<script lang="ts">
  /**
   * Autopublish links: point a page at a file on disk and keep it rendered.
   *
   * Two things this surface has to get right, both of which a naive list gets
   * wrong. A create whose *first render* failed comes back as a resolved
   * promise carrying `published: false` — so success is a field to read, not
   * the absence of a throw. And a link stores no health at all (links are
   * configuration, not status), so "is this file current" is derived here by
   * comparing the link's last published revision against the diagram's.
   */
  import {
    ago, autopublish, autopublishNow, autopublishStop,
    type Diagram, type Link, type Page, type PublishResult,
  } from './api';

  interface Props {
    save: string;
    pages: Page[];
    diagrams: Diagram[];
    links: Link[];
    all: boolean;
    onchanged: () => void;
    onerror: (message: string | null) => void;
  }

  let {
    save, pages, diagrams, links, all = $bindable(), onchanged, onerror,
  }: Props = $props();

  const FORMATS = ['svg', 'pdf', 'png', 'jpg'];

  let page = $state('');
  let target = $state('');
  let format = $state('svg');
  let busy = $state(false);
  let note = $state<string | null>(null);

  // A selected page can be renamed away underneath the form (or the selection
  // can outlive a diagram switch), and a bound value with no matching option
  // renders as a blank select that looks broken. Fall back to whole document.
  $effect(() => {
    if (page && !pages.some((p) => p.name === page)) page = '';
  });

  // Grouped only when the list is global — one diagram's links need no heading.
  let groups = $derived.by(() => {
    if (!all) return [{ save, links }];
    const by = new Map<string, Link[]>();
    for (const link of links) {
      if (!by.has(link.save)) by.set(link.save, []);
      by.get(link.save)!.push(link);
    }
    return [...by.entries()].sort().map(([s, l]) => ({ save: s, links: l }));
  });

  /**
   * Why a row is not current. Derived, never stored: the service deliberately
   * records nothing about a failure on the link itself.
   *
   * The revision check fires briefly and harmlessly after every edit — renders
   * are debounced a few seconds — so it is worded as "not yet", not as broken.
   */
  function warning(link: Link): string | null {
    const diagram = diagrams.find((d) => d.save === link.save);
    if (!diagram) return null;
    if (link.page && !diagram.pages.some((p) => p.name === link.page)) {
      return `page '${link.page}' is gone — the last render is kept`;
    }
    if (!link.last_published_at) {
      return 'never published — "now" reports why';
    }
    if (diagram.rev && link.last_rev !== diagram.rev) {
      return 'not yet republished at the current revision';
    }
    return null;
  }

  /** Surface a reported (not thrown) failure. */
  function report(result: PublishResult | undefined, what: string): void {
    if (!result) return;
    if (result.published) {
      note = `${what}: ${result.target}`;
      onerror(null);
    } else {
      onerror(`${what} did not render: ${result.reason ?? 'unknown reason'}` +
        (result.deleted ? ' (the link was dropped)' : ''));
    }
  }

  async function onStart(): Promise<void> {
    const path = target.trim();
    if (!path) return;
    if (!path.toLowerCase().endsWith(`.${format}`)) {
      // The service refuses this too; saying it here saves a round trip and a
      // stack-trace-shaped error message.
      onerror(`a ${format} link needs a target ending in .${format}`);
      return;
    }
    busy = true;
    note = null;
    try {
      const link = await autopublish(save, path, page || null, format);
      target = '';
      report(link.first_publish, 'published');
      onchanged();
    } catch (e) {
      onerror(e instanceof Error ? e.message : String(e));
    } finally {
      busy = false;
    }
  }

  async function onNow(link: Link): Promise<void> {
    busy = true;
    note = null;
    try {
      const result = await autopublishNow(link.id);
      report(result.results[0], 'republished');
      onchanged();
    } catch (e) {
      onerror(e instanceof Error ? e.message : String(e));
    } finally {
      busy = false;
    }
  }

  async function onStop(link: Link): Promise<void> {
    const ok = window.confirm(
      `Stop publishing ${link.save} to ${link.target}?\n\n` +
      'The file already published is LEFT on disk — something may be ' +
      'consuming it. This only stops future renders.',
    );
    if (!ok) return;
    busy = true;
    try {
      await autopublishStop(link.id);
      note = null;
      onchanged();
    } catch (e) {
      onerror(e instanceof Error ? e.message : String(e));
    } finally {
      busy = false;
    }
  }
</script>

<div class="head">
  <p class="hint">
    The file stays rendered: every accepted change to the diagram re-renders it.
    One way — nothing is ever read back. A link takes the same
    <code>swap</code> and <code>crop</code> a page-view URL takes, so a
    published file is the bytes that link serves; pass them from the CLI, e.g.
    <code>drawio autopublish … swap=ff00ff:00aa55 crop=frame-a</code>.
  </p>
  <label class="toggle">
    <input type="checkbox" bind:checked={all} onchange={() => onchanged()} />
    all diagrams
  </label>
</div>

<form onsubmit={(e) => { e.preventDefault(); onStart(); }}>
  <div class="line">
    <label>
      page
      <select bind:value={page} disabled={busy}>
        <option value="">whole document</option>
        {#each pages as p (p.id ?? p.name)}
          {#if p.name}<option value={p.name}>{p.name}</option>{/if}
        {/each}
      </select>
    </label>
    <label>
      format
      <select bind:value={format} disabled={busy}>
        {#each FORMATS as f (f)}<option value={f}>{f}</option>{/each}
      </select>
    </label>
  </div>
  <div class="line">
    <label class="grow">
      file
      <input
        bind:value={target}
        placeholder="/absolute/path/figure.{format}"
        disabled={busy}
      />
    </label>
    <button type="submit" disabled={busy || !target.trim()}>start</button>
  </div>
</form>

{#if note}<p class="note">{note}</p>{/if}

{#if links.length === 0}
  <p class="empty">
    {all ? 'No autopublish links on this host.' : 'No links on this diagram.'}
  </p>
{:else}
  {#each groups as group (group.save)}
    {#if all}<h4>{group.save}</h4>{/if}
    <ul>
      {#each group.links as link (link.id)}
        <li>
          <div class="row">
            <span class="target" title={link.target}>{link.target}</span>
            <span class="buttons">
              <button onclick={() => onNow(link)} disabled={busy}>now</button>
              <button onclick={() => onStop(link)} disabled={busy}>stop</button>
            </span>
          </div>
          <p class="meta">
            {link.format} · {link.page ?? 'whole document'}
            {#if link.last_rev}· <code>{link.last_rev.slice(0, 8)}</code>{/if}
            · {link.last_published_at ? ago(link.last_published_at) : 'never'}
          </p>
          {#if warning(link)}
            <p class="warn">⚠ {warning(link)}</p>
          {/if}
        </li>
      {/each}
    </ul>
  {/each}
{/if}

<style>
  .head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }
  .hint { font-size: 0.78rem; color: var(--text3, #888); margin: 0 0 0.6rem; }
  .toggle {
    flex: 0 0 auto;
    font-size: 0.78rem;
    color: var(--text2, #bbb);
    display: flex;
    align-items: center;
    gap: 0.3rem;
    white-space: nowrap;
  }

  form { display: flex; flex-direction: column; gap: 0.4rem; margin-bottom: 0.8rem; }
  .line { display: flex; gap: 0.5rem; align-items: flex-end; }
  /* Scoped to the form: an unscoped `label` rule also caught `.toggle` and
     stacked its checkbox above its text. */
  form label {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    font-size: 0.72rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text3, #888);
  }
  form label.grow { flex: 1 1 auto; min-width: 0; }
  form input, form select {
    background: var(--surface2, #1c1c1c);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text, #ddd);
    font: inherit;
    font-size: 0.8rem;
    text-transform: none;
    letter-spacing: normal;
    padding: 0.25rem 0.4rem;
    min-width: 0;
  }

  button {
    background: var(--surface2, #1c1c1c);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text2, #bbb);
    font: inherit;
    font-size: 0.78rem;
    padding: 0.25rem 0.6rem;
    cursor: pointer;
  }
  button:hover:not(:disabled) { background: var(--surface3, #2a2a2a); color: var(--text, #ddd); }
  button:disabled { opacity: 0.5; cursor: default; }

  h4 {
    font-size: 0.75rem;
    font-family: var(--mono, monospace);
    color: var(--text3, #888);
    margin: 0.9rem 0 0.2rem;
  }
  ul { list-style: none; margin: 0; padding: 0; }
  li { padding: 0.4rem 0; border-bottom: 1px solid var(--border, #262626); }
  li:last-child { border-bottom: none; }

  .row { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; }
  .target {
    font-family: var(--mono, monospace);
    font-size: 0.8rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .buttons { display: flex; gap: 0.3rem; flex: 0 0 auto; }

  .meta { font-size: 0.72rem; color: var(--text3, #888); margin: 0.1rem 0 0; }
  code { font-family: var(--mono, monospace); }
  .warn { font-size: 0.75rem; color: #d68b5f; margin: 0.15rem 0 0; }
  .note { font-size: 0.78rem; color: #7bd88f; margin: 0 0 0.6rem; }
  .empty { color: var(--text3, #888); font-size: 0.85rem; margin: 0.5rem 0; }
</style>
