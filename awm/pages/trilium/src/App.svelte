<script lang="ts">
  // The reception page for the Trilium fleet: who has an instance, is theirs
  // healthy, and a way in to each.
  //
  // A list rather than a dashboard, because the fleet is the unit here. One
  // person's server being down says nothing about anyone else's, so each row
  // carries its own state and its own controls, and the page-level banner only
  // reports the one thing they share: which bundle is being served.
  //
  // Three ways a row can be down and they need three different fixes — the
  // node process, that user's TLS listener, and the bundle nobody built — so
  // they are shown apart rather than as one green dot.
  import { onMount, onDestroy } from 'svelte';
  import * as api from './lib/api';

  let st = $state<api.Status | null>(null);
  let error = $state<string | null>(null);
  let busy = $state<string | null>(null);
  let log = $state<string>('');
  let openLog = $state<string | null>(null);
  let timer: ReturnType<typeof setInterval> | null = null;

  async function refresh() {
    try {
      st = await api.status();
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  async function act(key: string, fn: () => Promise<unknown>) {
    if (busy) return;
    busy = key;
    try {
      await fn();
      await refresh();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = null;
    }
  }

  async function toggleLog(user: string) {
    if (openLog === user) { openLog = null; return; }
    openLog = user;
    log = (await api.logs(user, 200)).log;
  }

  function open(url: string) {
    // Cross-origin by construction: this page is on the gateway front, each
    // Trilium on its own. A new tab, never an iframe.
    window.open(url, '_blank');
  }

  onMount(() => {
    refresh();
    timer = setInterval(refresh, 10_000);
  });
  onDestroy(() => { if (timer) clearInterval(timer); });

  const src = $derived(st?.source ?? null);
  const frontOf = $derived((user: string) =>
    st?.fronts.find((f) => f.user === user) ?? null);
  // Reachable means all three: the process is up, it bound its port, and that
  // user's front is serving. Any one missing and the button lies.
  const reachable = $derived((user: string) => {
    const i = st?.instances.find((x) => x.user === user);
    return !!i?.listening && !!frontOf(user)?.serving;
  });
  const anyone = $derived((st?.instances.length ?? 0) > 0);
</script>

<main>
  <header>
    <h1>Trilium</h1>
    <span class="badge">{st?.instances.length ?? 0} instance(s)</span>
  </header>

  <p class="hint">
    One server per person, each with its own database and its own login. That
    second login is what tells two people apart — the awm session in front of
    it is one shared password for the whole workspace.
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

  {#if st && anyone}
    {#each st.instances as inst (inst.user)}
      {@const front = frontOf(inst.user)}
      <section data-state={reachable(inst.user) ? 'up' : 'down'}>
        <h2>{inst.user}</h2>

        <button
          class="primary"
          onclick={() => front?.url && open(front.url)}
          disabled={!reachable(inst.user)}
        >
          Open {inst.user}'s notes
        </button>
        <p class="hint">
          Opens in a new tab{front?.url ? ` (${front.url})` : ''}. You will be
          asked for this instance's own password.
        </p>

        <ul class="steps">
          <li data-ok={inst.running}>
            server running{inst.exit_code != null && !inst.running
              ? ` (last exit ${inst.exit_code})` : ''}
          </li>
          <li data-ok={inst.listening}>listening on :{inst.port} <span class="dim">(loopback)</span></li>
          <li data-ok={front?.serving && front?.tls}>
            front serving <span class="dim">:{front?.listener_port}</span>
            {#if front?.error}<span class="error"> {front.error}</span>{/if}
          </li>
          <li data-ok={inst.backups.length > 0}>
            recoverable copies
            <span class="dim">{inst.backups.join(', ') || 'none yet'}</span>
          </li>
        </ul>

        <dl>
          <dt>uptime</dt>
          <dd>{inst.uptime_s != null ? `${Math.floor(inst.uptime_s / 60)} min` : '—'}</dd>
          <dt>content</dt><dd class="dim">{inst.scope}</dd>
          {#if inst.error}<dt>error</dt><dd class="error">{inst.error}</dd>{/if}
        </dl>

        <div class="row">
          <button onclick={() => act(`start:${inst.user}`, () => api.start(inst.user))}
                  disabled={!!busy || inst.running}>Start</button>
          <button onclick={() => act(`restart:${inst.user}`, () => api.restart(inst.user))}
                  disabled={!!busy}>Restart</button>
          <button onclick={() => act(`stop:${inst.user}`, () => api.stop(inst.user))}
                  disabled={!!busy || !inst.running}>Stop</button>
          <button onclick={() => toggleLog(inst.user)} disabled={!!busy}>
            {openLog === inst.user ? 'Hide log' : 'Log'}
          </button>
        </div>
        {#if openLog === inst.user}<pre class="log">{log || '(empty)'}</pre>{/if}
      </section>
    {/each}

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
        Notes are stored as HTML, and the markdown tree in each person's scope is
        an export of it. Read and diff that tree freely. Recover from a note
        revision or from one of the database copies above, never from the
        markdown.
      </p>
    </section>
  {:else if st}
    <p class="warn">
      Nobody has an instance yet. A person exists here because a scope exists:
      <code>awm scope create --project userdata --scope trilium/&lt;user&gt;
      --branch-name trilium/&lt;user&gt; --from-branch main</code>. The
      supervision loop picks it up without a restart.
    </p>
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
  section[data-state='up'] h2 { color: var(--ok, #4ade80); }
  section[data-state='down'] h2 { color: var(--warn, #fbbf24); }

  button {
    font: inherit; padding: 0.4rem 0.8rem; border-radius: 6px;
    border: 1px solid var(--border, #3a3a3a);
    background: var(--surface, #1e1e1e); color: inherit; cursor: pointer;
  }
  button:disabled { opacity: 0.45; cursor: default; }
  button.primary {
    width: 100%; padding: 0.7rem; font-weight: 600;
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
  .log {
    max-height: 22rem; overflow: auto; font-size: 0.8rem;
    background: var(--surface, #1a1a1a); padding: 0.75rem; border-radius: 6px;
  }
</style>
