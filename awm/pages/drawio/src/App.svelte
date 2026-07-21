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
  import {
    ago, buildTree, check, checkoutUrl, create, editorUrl, history, info, list,
    restore, size,
    type CheckReport, type Diagram, type Revision, type TreeNode,
  } from '$lib/api';

  let diagrams = $state<Diagram[]>([]);
  let tree = $state<TreeNode[]>([]);
  let selected = $state<string | null>(null);
  let detail = $state<Diagram | null>(null);
  let revisions = $state<Revision[]>([]);
  let report = $state<CheckReport | null>(null);
  let error = $state<string | null>(null);
  let busy = $state(false);
  let newPath = $state('');

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

        <h3>Pages</h3>
        <ul class="pages">
          {#each detail.pages as page (page.id ?? page.name)}
            <li>
              <span>{page.name ?? '(unnamed)'}</span>
              <span class="count">{page.cells} cells</span>
            </li>
          {/each}
        </ul>

        <h3>History</h3>
        {#if revisions.length === 0}
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
  main {
    padding: 1.5rem;
    font-family: system-ui, sans-serif;
    color: var(--text, #ddd);
    background: var(--bg, #111);
    min-height: 100vh;
  }
  header { margin-bottom: 1rem; }
  h1 {
    font-size: 1.2rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    margin: 0 0 0.25rem;
  }
  h2 { font-size: 1rem; margin: 0 0 0.2rem; font-family: var(--mono, monospace); }
  h3 {
    font-size: 0.8rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text2, #bbb);
    margin: 1.2rem 0 0.4rem;
  }
  .hint { font-size: 0.85rem; color: var(--text3, #888); margin: 0; }

  .split {
    display: grid;
    grid-template-columns: minmax(18rem, 26rem) 1fr;
    gap: 1.5rem;
    align-items: start;
  }
  @media (max-width: 46rem) {
    .split { grid-template-columns: 1fr; }
  }

  section {
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 0.75rem;
    background: var(--surface, #161616);
  }

  .empty { color: var(--text3, #888); font-size: 0.85rem; margin: 0.5rem 0; }
  .meta { font-size: 0.75rem; color: var(--text3, #888); margin: 0 0 0.6rem; }
  code { font-family: var(--mono, monospace); font-size: 0.9em; }

  .error {
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

  .create { display: flex; gap: 0.4rem; margin-top: 0.9rem; }
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
