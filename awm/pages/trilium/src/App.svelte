<script lang="ts">
  // The reception page for the shared vault: is it up, does it have a
  // database, is there a copy to recover from, and a way in.
  //
  // One panel, because there is one vault. It reports rather than controls:
  // every verb that acts on the vault is refused for a caller arriving through
  // an edge, because the vault is shared and those are one person's button on
  // everyone's work. What is left is the state somebody would want before
  // trusting it, and each of the four ways it can be down is shown apart,
  // because they need four different fixes.
  import { onMount, onDestroy } from 'svelte';
  import * as api from './lib/api';

  let st = $state<api.Status | null>(null);
  let error = $state<string | null>(null);
  let snaps = $state<api.SnapshotInfo[] | null>(null);
  let timer: ReturnType<typeof setInterval> | null = null;

  async function refresh() {
    try {
      st = await api.status();
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  async function toggleSnaps() {
    if (snaps) { snaps = null; return; }
    try {
      snaps = (await api.snapshots()).snapshots;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  onMount(() => {
    refresh();
    timer = setInterval(refresh, 10_000);
  });
  onDestroy(() => { if (timer) clearInterval(timer); });

  const src = $derived(st?.source ?? null);
  const v = $derived(st?.vault ?? null);
  // Reachable means both: the process is up and it bound its port. A start
  // that failed to bind is running and useless, and the link would lie.
  const reachable = $derived(!!v?.listening);
</script>

<main>
  <header>
    <h1>Vault</h1>
    <span class="badge">{reachable ? 'up' : 'down'}</span>
  </header>

  <p class="hint">
    One knowledge base, shared by everyone who can sign in. There is no second
    password: you are already signed in, and the vault is served on this origin
    behind that same session.
  </p>

  {#if error}<p class="error">{error}</p>{/if}

  {#if src && !src.entry}
    <p class="warn">
      No server bundle installed. Run <code>awm/services/trilium/install.sh</code>
      — it builds the fork at {src.fork_dir}, or downloads the published build
      when there is none.
    </p>
  {:else if src?.built_current === false}
    <p class="warn">
      The bundle being served was built from {src.built_head?.slice(0, 8) ?? '—'},
      and the worktree is at {src.describe ?? src.head?.slice(0, 8) ?? '—'}. Re-run
      install.sh.
    </p>
  {/if}

  {#if st && v}
    <section data-state={reachable ? 'up' : 'down'}>
      <a class="primary" href={api.VAULT_PATH} aria-disabled={!reachable}>
        Open the vault
      </a>
      <p class="hint">
        Same origin, same session — no new tab and nothing further to sign in to.
      </p>

      <ul class="steps">
        <li data-ok={v.running}>server running</li>
        <li data-ok={v.listening}>listening <span class="dim">(loopback only)</span></li>
        <li data-ok={v.initialized !== false}>
          {v.initialized === false ? 'no database yet — it is being created' : 'database ready'}
        </li>
        <li data-ok={v.snapshots > 0}>
          {v.snapshots} pinned snapshot(s)
          <span class="dim">
            · {v.backups.length} rolling copy(ies), overwritten on a schedule
          </span>
        </li>
      </ul>

      <dl>
        <dt>uptime</dt>
        <dd>{v.uptime_s != null ? `${Math.floor(v.uptime_s / 60)} min` : '—'}</dd>
        {#if v.error}<dt>error</dt><dd class="error">{v.error}</dd>{/if}
      </dl>

      <div class="row">
        <button onclick={toggleSnaps}>{snaps ? 'Hide copies' : 'Copies'}</button>
      </div>

      {#if snaps}
        {#if snaps.length === 0}
          <p class="hint">No copies yet.</p>
        {:else}
          <table>
            <tbody>
              {#each snaps as s (s.file)}
                <tr>
                  <td>{s.kind === 'snapshot' ? 'pinned' : 'rolling'}</td>
                  <td class="dim">{s.modified}</td>
                  <td>{s.name}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
        <p class="hint">
          Taking a copy is <code>awm trilium snapshot</code> and putting one back
          is <code>awm trilium restore --snapshot &lt;name&gt; --confirm</code>,
          both on the host. A restore replaces the whole vault and every note
          anyone has written since, which is why it is a verb and not a button.
        </p>
      {/if}
    </section>

    <section>
      <h2>Serving</h2>
      <dl>
        <dt>bundle</dt>
        <dd class="dim">{src?.entry ?? '—'}</dd>
        <dt>from</dt>
        <dd>
          {src?.source === 'fork' ? 'the fork we build' : 'the published build'}
          {#if src?.describe}<span class="dim"> · {src.describe}</span>{/if}
        </dd>
      </dl>
      <p class="hint">
        Notes are stored as HTML, and the markdown tree in the vault's scope is
        an export of it. Read and diff that tree freely. Recover from a pinned
        snapshot, never from the markdown — the export is a conversion, and
        converting it back loses formatting.
      </p>
    </section>
  {:else if !error}
    <p class="hint">Loading…</p>
  {/if}
</main>

<style>
  main {
    max-width: 44rem;
    margin: 0 auto;
    padding: 2rem 1.25rem 4rem;
    font: 15px/1.5 var(--font-sans, system-ui, sans-serif);
    color: var(--text, #e6e6e6);
  }
  header { display: flex; align-items: baseline; gap: 0.75rem; }
  h1 { font-size: 1.4rem; margin: 0 0 0.5rem; }
  h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em;
       color: var(--text-dim, #9a9a9a); margin: 2rem 0 0.5rem; }
  .badge {
    font-size: 0.75rem; padding: 0.1rem 0.5rem; border-radius: 999px;
    border: 1px solid currentColor; color: var(--text-dim, #9a9a9a);
  }
  /* The vault panel has no heading of its own — the page is the vault — so
     the state colours the link into it instead. */
  section[data-state='down'] .primary { opacity: 0.5; pointer-events: none; }

  button {
    font: inherit; padding: 0.4rem 0.8rem; border-radius: 6px;
    border: 1px solid var(--border, #3a3a3a);
    background: var(--surface, #1e1e1e); color: inherit; cursor: pointer;
  }
  button:disabled { opacity: 0.45; cursor: default; }
  /* An anchor, not a button: the vault is a page on this origin, so it should
     middle-click, bookmark and open in a new tab like any other link. */
  .primary {
    display: block; text-align: center; text-decoration: none;
    width: 100%; padding: 0.7rem; font-weight: 600; border-radius: 6px;
    background: var(--accent, #2f6fed); border-color: transparent; color: #fff;
  }
  .row { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.75rem; }

  dl { display: grid; grid-template-columns: 8rem 1fr; gap: 0.25rem 1rem; margin: 0.75rem 0 0; }
  dt { color: var(--text-dim, #9a9a9a); }
  dd { margin: 0; }

  .steps { list-style: none; padding: 0; margin: 0.75rem 0 0; }
  .steps li { padding-left: 1.4rem; position: relative; margin: 0.2rem 0; }
  /* The marker carries the state, so a screenshot of this page is readable
     without colour vision: ✓ / ✗, not two shades of dot. */
  .steps li::before { content: '✗'; position: absolute; left: 0; color: var(--warn, #fbbf24); }
  .steps li[data-ok='true']::before { content: '✓'; color: var(--ok, #4ade80); }

  code { font-size: 0.85em; background: var(--surface, #1a1a1a); padding: 0.1em 0.3em;
         border-radius: 4px; }
  .dim { color: var(--text-dim, #9a9a9a); }
  .warn { color: var(--warn, #fbbf24); }
  .hint { color: var(--text-dim, #9a9a9a); font-size: 0.85rem; }
  .error { color: var(--error, #f87171); }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-top: 0.5rem; }
  td { padding: 0.15rem 0.5rem 0.15rem 0; vertical-align: top; }
  td:first-child { width: 5rem; color: var(--text-dim, #9a9a9a); }
</style>
