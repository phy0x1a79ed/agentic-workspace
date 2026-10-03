<script lang="ts">
  /**
   * Diagram reception page — the front door for the drawio service.
   *
   * Left: every saved diagram in its folder structure, with any checkouts
   * nested underneath. Right: the selected diagram's pages, its revision
   * history with restore, and an image-reference check.
   *
   * History replaces what the prototype did with roughly fifty hand-named
   * `.bak` files, so restoring is a click rather than an archaeology exercise.
   */
  import { onMount } from 'svelte';
  import DiagramTree from '$lib/DiagramTree.svelte';
  import PublishPanel from '$lib/PublishPanel.svelte';
  import { UserChip } from '@awm/primitives';
  import {
    absolute, ago, autopublishList, buildTree, check, checkoutUrl, copyText,
    create, editorUrl, history, imageCellXml, info, list, renderSize, restore,
    size, viewUrl,
    type CheckReport, type Diagram, type Link, type Revision, type TreeNode,
  } from '$lib/api';

  // Illustrative, not this diagram's: the note is documentation, and a URL you
  // are meant to read beats one you might paste unread. The real one comes from
  // `copy url`, which is the only thing that knows the prefix and the encoding.
  const EXAMPLE_URL =
    '/drawio-app/view/<folder>/<diagram>/<page>' +
    '?swap=ff00ff:00aa55&swap=00ff00:333333&crop=<label>';

  let diagrams = $state<Diagram[]>([]);
  let tree = $state<TreeNode[]>([]);
  let selected = $state<string | null>(null);
  let detail = $state<Diagram | null>(null);
  let revisions = $state<Revision[]>([]);
  let links = $state<Link[]>([]);
  let report = $state<CheckReport | null>(null);
  let error = $state<string | null>(null);
  let busy = $state(false);
  let newPath = $state('');

  // The tab lives here rather than per-diagram, so switching diagrams keeps
  // you on the tab you were reading.
  let tab = $state<'pages' | 'publish' | 'history'>('pages');
  let allLinks = $state(false);
  let copied = $state<string | null>(null);
  //  Shown when the clipboard is unavailable — the gateway is plain HTTP unless
  //  it is fronted by httpsfront, and `navigator.clipboard` needs a secure
  //  context. A silent no-op here would break the whole flow invisibly.
  let manual = $state<{ label: string; text: string } | null>(null);

  async function refresh() {
    try {
      const result = await list();
      diagrams = result.diagrams;
      tree = buildTree(diagrams);
      error = null;
      if (selected && !diagrams.some((d) => d.save === selected)) selected = null;
      if (selected) await loadDetail(selected);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  async function loadDetail(save: string) {
    report = null;
    try {
      detail = await info(save);
      revisions = (await history(save)).revisions;
      // Rides the existing 5s poll, so a link created, stopped, or dropped by
      // the service (its target folder went away) shows up without a second
      // timer. Its own try: the public edge does not expose autopublish, and
      // that must not blank the pages and history that did load.
      try {
        links = (await autopublishList(allLinks ? undefined : save)).links;
      } catch {
        links = [];
      }
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  async function select(save: string) {
    selected = save;
    await loadDetail(save);
  }

  async function open(save: string) {
    window.open(await editorUrl(save), '_blank');
  }

  async function openCheckout(handle: string) {
    window.open(await checkoutUrl(handle), '_blank');
  }

  async function onCreate() {
    const path = newPath.trim();
    if (!path) return;
    busy = true;
    try {
      const result = await create(path);
      newPath = '';
      await refresh();
      await select(result.save);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function onRestore(rev: Revision) {
    if (!selected) return;
    // Forward-only: this lands as a new revision, so the state being replaced
    // stays in history and the restore itself can be undone. Worth saying out
    // loud, because "restore" reads as destructive.
    const ok = window.confirm(
      `Restore ${selected} to "${rev.label}" (${rev.rev.slice(0, 8)})?\n\n` +
      'This lands as a NEW revision — nothing is erased, and you can restore ' +
      'back to the current state afterwards.',
    );
    if (!ok) return;
    busy = true;
    try {
      await restore(selected, rev.rev);
      await refresh();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  /** Hand `text` to the clipboard, or show it for manual copying. */
  async function offer(row: string, kind: string, text: string) {
    manual = null;
    if (await copyText(text)) {
      copied = row;
      setTimeout(() => { if (copied === row) copied = null; }, 2500);
    } else {
      manual = { label: `${row} (${kind})`, text };
    }
  }

  /**
   * Copy a paste-ready image cell pointing at the page's live SVG.
   *
   * The fetch is a real render on a cold cache and can take seconds — which is
   * also the point: a page that will not render fails here, loudly, instead of
   * handing over a URL that 404s once it has been placed.
   */
  async function copyCell(page: string | null, label: string) {
    if (!detail) return;
    busy = true;
    try {
      const url = absolute((await viewUrl(detail.save, page)).url);
      const { width, height } = await renderSize(url);
      await offer(label, 'cell', imageCellXml(url, width, height));
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function copyViewUrl(page: string | null, label: string) {
    if (!detail) return;
    busy = true;
    try {
      await offer(label, 'url', absolute((await viewUrl(detail.save, page)).url));
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function onCheck() {
    if (!selected) return;
    busy = true;
    try {
      report = await check(selected);
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    refresh();
    // Cheap liveness: pick up new diagrams, editor tabs and checkouts without
    // making the user reload. The list verb is small (no XML).
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  });
</script>

<main>
  <header>
    <h1>Diagrams</h1>
    <UserChip links={[{ label: 'notes', href: '/ui/notes/' }]} />
    <p class="hint">
      Click to inspect, double-click or <em>open</em> to edit. Checkouts are
      agents' in-flight work — open one to review it before it lands.
    </p>
  </header>

  {#if error}
    <div class="error">{error}</div>
  {/if}

  <div class="split">
    <section class="tree">
      <div class="tree-scroll">
        {#if diagrams.length === 0}
          <p class="empty">No diagrams yet.</p>
        {:else}
          <DiagramTree
            nodes={tree}
            {selected}
            onselect={select}
            onopen={open}
            onopencheckout={openCheckout}
          />
        {/if}
      </div>

      <form class="create" onsubmit={(e) => { e.preventDefault(); onCreate(); }}>
        <input
          bind:value={newPath}
          placeholder="new/diagram/path"
          aria-label="New diagram path"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !newPath.trim()}>create</button>
      </form>
    </section>

    <section class="detail">
      {#if !detail}
        <p class="empty">Select a diagram.</p>
      {:else}
        <h2>{detail.save}</h2>
        <p class="meta">
          {size(detail.bytes)} · {ago(detail.modified)}
          {#if detail.author}· {detail.author}{/if}
          {#if detail.rev}· <code>{detail.rev.slice(0, 8)}</code>{/if}
        </p>

        <div class="actions">
          <button onclick={() => open(detail!.save)}>open editor</button>
          <button onclick={onCheck} disabled={busy}>check images</button>
        </div>

        {#if report}
          <div class="report" class:bad={!report.ok}>
            {#if report.ok}
              {report.references} image reference{report.references === 1 ? '' : 's'}, all resolving.
            {:else}
              <strong>{report.problems.length} broken reference(s)</strong>
              <ul>
                {#each report.problems as p (p.path)}
                  <li>
                    <code>{p.path}</code> — {p.problem}
                    <span class="fix">{p.fix}</span>
                  </li>
                {/each}
              </ul>
            {/if}
          </div>
        {/if}

        <div class="tabs" role="tablist">
          {#each [['pages', 'pages'], ['publish', 'publish'], ['history', 'history']] as [id, label] (id)}
            <button
              role="tab"
              aria-selected={tab === id}
              class:active={tab === id}
              onclick={() => (tab = id as typeof tab)}
            >{label}</button>
          {/each}
        </div>

        {#if manual}
          <div class="manual">
            <p>
              The clipboard is unavailable here (it needs a secure origin) —
              select and copy <strong>{manual.label}</strong> by hand:
            </p>
            <!-- svelte-ignore a11y_autofocus -->
            <textarea readonly autofocus onfocus={(e) => e.currentTarget.select()}
              >{manual.text}</textarea>
            <button onclick={() => (manual = null)}>dismiss</button>
          </div>
        {/if}

        {#if tab === 'pages'}
          <p class="hint">
            Copy a page as a cell and paste it into another diagram — the placed
            image re-renders whenever this page changes.
          </p>
          <!-- Collapsed by default: the note is worth finding, but the page
               list is what this tab is for and must stay above the fold. -->
          <details class="recipe">
            <summary>one page, many colours and framings</summary>
            <p>
              The URL is the variable, so you do not need a near-duplicate page
              per colour. Draw the region that should vary in
              <code>#ff00ff</code>, then add <code>swap=from:to</code> — bare
              hex, repeatable, applied simultaneously. Draw a rectangle around a
              region and label it, then add <code>crop=that-label</code>; the
              rectangle itself never appears in the render. Append either to the
              URL from <em>copy url</em>:
            </p>
            <textarea class="example" readonly rows="2"
              onfocus={(e) => e.currentTarget.select()}
              >{EXAMPLE_URL}</textarea>
            <p class="fine">
              Swaps reach style colours, label colours and referenced SVGs — not
              raster images, where a pink region stays pink. Both parameters
              survive export: the exporter renders the referenced page and
              embeds it, so the file works off this machine.
            </p>
          </details>
          <ul class="pages">
            {#each [null, ...detail.pages] as page (page ? (page.id ?? page.name) : '__whole__')}
              {@const name = page ? (page.name ?? '(unnamed)') : 'whole document'}
              {@const key = page ? page.name : null}
              <li>
                <div class="page">
                  <span class="label">{name}</span>
                  {#if page}<span class="count">{page.cells} cells</span>{/if}
                </div>
                <span class="buttons">
                  {#if copied === name}<span class="ok">copied</span>{/if}
                  <button
                    onclick={() => copyCell(key, name)}
                    disabled={busy || (!!page && !page.name)}
                    title="Paste into a drawio tab with Ctrl-V"
                  >copy cell</button>
                  <button
                    onclick={() => copyViewUrl(key, name)}
                    disabled={busy || (!!page && !page.name)}
                    title="For Insert > Image > URL (choose Link, not a copy)"
                  >copy url</button>
                </span>
              </li>
            {/each}
          </ul>
        {:else if tab === 'publish'}
          <PublishPanel
            save={detail.save}
            pages={detail.pages}
            {diagrams}
            {links}
            bind:all={allLinks}
            onchanged={() => refresh()}
            onerror={(m: string | null) => (error = m)}
          />
        {:else if revisions.length === 0}
          <p class="empty">No revisions.</p>
        {:else}
          <ul class="history">
            {#each revisions as rev, i (rev.rev)}
              <li>
                <div class="rev">
                  <span class="label">{rev.label}</span>
                  <span class="meta">
                    {rev.author} · {ago(rev.when)} · <code>{rev.rev.slice(0, 8)}</code>
                  </span>
                </div>
                {#if i > 0}
                  <button onclick={() => onRestore(rev)} disabled={busy}>restore</button>
                {:else}
                  <span class="current">current</span>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    </section>
  </div>
</main>

<style>
  /* The shell is locked to the viewport and the two panels scroll inside it.
     Letting the document scroll instead would move the header off-screen while
     the tree — the thing you are actually scrolling — stays put. */
  main {
    display: flex;
    flex-direction: column;
    height: 100dvh;
    overflow: hidden;
    padding: 1.5rem;
    font-family: var(--sans);
    color: var(--text);
    background: var(--bg);
  }
  header { flex: 0 0 auto; margin-bottom: 1rem; }
  h1 {
    font-size: 1.2rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    margin: 0 0 0.25rem;
  }
  h2 { font-size: 1rem; margin: 0 0 0.2rem; font-family: var(--mono, monospace); }
  .hint { font-size: 0.85rem; color: var(--text3, #888); margin: 0 0 0.6rem; }

  .split {
    display: grid;
    grid-template-columns: minmax(18rem, 26rem) 1fr;
    /* An explicit row that fills the remaining height — an implicit row sizes to
       its content, which would leave the panels short and hand the scroll back
       to the document. minmax(0,…) lets them shrink below their content. */
    grid-template-rows: minmax(0, 1fr);
    gap: 1.5rem;
    flex: 1 1 auto;
    min-height: 0;
  }

  section {
    display: flex;
    flex-direction: column;
    min-height: 0;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.75rem;
    background: var(--surface);
  }

  /* The tree scrolls; the create form stays pinned to the bottom of the panel. */
  .tree-scroll { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
  .detail { overflow-y: auto; }

  /* One column on a phone — and there the document scrolls, because a locked
     viewport with two stacked panels gives each one a uselessly short window. */
  @media (max-width: 46rem) {
    main { height: auto; min-height: 100dvh; overflow: visible; }
    .split { grid-template-columns: 1fr; grid-template-rows: none; }
    section, .tree-scroll, .detail { overflow: visible; }
  }

  .empty { color: var(--text3, #888); font-size: 0.85rem; margin: 0.5rem 0; }
  .meta { font-size: 0.75rem; color: var(--text3, #888); margin: 0 0 0.6rem; }
  code { font-family: var(--mono, monospace); font-size: 0.9em; }

  .error {
    flex: 0 0 auto;
    border: 1px solid #a8552f;
    background: #2a1a12;
    color: #d68b5f;
    border-radius: 4px;
    padding: 0.5rem 0.75rem;
    margin-bottom: 1rem;
    font-size: 0.85rem;
  }

  button {
    background: var(--surface2, #1c1c1c);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text2, #bbb);
    font: inherit;
    font-size: 0.78rem;
    padding: 0.2rem 0.6rem;
    cursor: pointer;
  }
  button:hover:not(:disabled) { background: var(--surface3, #2a2a2a); color: var(--text, #ddd); }
  button:disabled { opacity: 0.5; cursor: default; }

  .create { flex: 0 0 auto; display: flex; gap: 0.4rem; margin-top: 0.9rem; }
  .create input {
    flex: 1 1 auto;
    min-width: 0;
    background: var(--surface2, #1c1c1c);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text, #ddd);
    font: inherit;
    font-size: 0.8rem;
    padding: 0.25rem 0.5rem;
  }

  .actions { display: flex; gap: 0.5rem; margin-bottom: 0.5rem; }

  .report {
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 0.5rem 0.7rem;
    font-size: 0.8rem;
    color: var(--text2, #bbb);
  }
  .report.bad { border-color: #a8552f; color: #d68b5f; }
  .report ul { margin: 0.4rem 0 0; padding-left: 1rem; }
  .report .fix { display: block; color: var(--text3, #888); font-size: 0.75rem; }

  /* The tab strip sits between the diagram-level actions (which stay above it,
     because they are about the diagram rather than any one tab) and the tab
     body. Underlined-active rather than boxed, so it reads as a divider. */
  .tabs {
    display: flex;
    gap: 0.15rem;
    border-bottom: 1px solid var(--border, #333);
    margin: 1rem 0 0.6rem;
  }
  .tabs button {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 0.3rem 0.7rem;
    font-size: 0.75rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text3, #888);
    margin-bottom: -1px;
  }
  .tabs button:hover:not(:disabled) { background: none; color: var(--text2, #bbb); }
  .tabs button.active {
    color: var(--text, #ddd);
    border-bottom-color: var(--text2, #bbb);
  }

  .manual {
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 0.5rem 0.7rem;
    margin-bottom: 0.7rem;
    font-size: 0.8rem;
    color: var(--text2, #bbb);
  }
  .manual p { margin: 0 0 0.4rem; }
  .manual textarea {
    width: 100%;
    height: 5rem;
    background: var(--surface2, #1c1c1c);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text, #ddd);
    font-family: var(--mono, monospace);
    font-size: 0.72rem;
    padding: 0.3rem;
    margin-bottom: 0.4rem;
    resize: vertical;
  }

  /* The parameter note. Same muted register as .hint and the same selectable
     textarea idiom as .manual, so the example can be copied without adding a
     second clipboard path. */
  .recipe {
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 0.4rem 0.7rem;
    margin: 0 0 0.7rem;
    font-size: 0.78rem;
    color: var(--text3, #888);
  }
  .recipe summary { cursor: pointer; color: var(--text2, #bbb); }
  .recipe p { margin: 0.5rem 0 0.4rem; line-height: 1.45; }
  .recipe p.fine { margin-bottom: 0.2rem; }
  .recipe code {
    font-family: var(--mono, monospace);
    font-size: 0.9em;
    color: var(--text2, #bbb);
  }
  .recipe textarea.example {
    width: 100%;
    background: var(--surface2, #1c1c1c);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text, #ddd);
    font-family: var(--mono, monospace);
    font-size: 0.72rem;
    padding: 0.3rem;
    resize: vertical;
  }

  .page { display: flex; flex-direction: column; gap: 0.1rem; min-width: 0; }
  .buttons { display: flex; align-items: center; gap: 0.3rem; flex: 0 0 auto; }
  .ok { font-size: 0.72rem; color: #7bd88f; }

  ul.pages, ul.history { list-style: none; margin: 0; padding: 0; }
  ul.pages li, ul.history li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.25rem 0;
    border-bottom: 1px solid var(--border, #262626);
    font-size: 0.85rem;
  }
  ul.pages li:last-child, ul.history li:last-child { border-bottom: none; }
  .count { color: var(--text3, #888); font-size: 0.78rem; }

  .rev { display: flex; flex-direction: column; gap: 0.1rem; min-width: 0; }
  .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .current { font-size: 0.72rem; color: var(--text3, #888); }
</style>
