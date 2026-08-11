<script lang="ts">
  // The workbench's reception page: is it healthy, what version, and a way in.
  //
  // Two audiences, one screen. Most visits are "take me to the workbench", so
  // the sign-in button is the first thing and everything else is beneath it.
  // The rest is for the visit where the button did *not* work, which is why the
  // three install steps and the two fronts are shown separately rather than
  // reduced to one green dot — "down" is not a diagnosis, and the whole reason
  // this service exists is that the hand-managed units could not tell you which
  // half was broken.
  import { onMount, onDestroy } from 'svelte';
  import * as api from './lib/api';

  let st = $state<api.Status | null>(null);
  let error = $state<string | null>(null);
  let busy = $state<string | null>(null);
  let log = $state<string>('');
  let showLog = $state(false);
  let timer: ReturnType<typeof setInterval> | null = null;

  async function refresh() {
    try {
      st = await api.status();
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  async function act(name: string, fn: () => Promise<unknown>) {
    if (busy) return;
    // stop/restart end live conversations, so they ask. start and the read-only
    // actions do not — a confirm on a harmless button trains people past it.
    if ((name === 'stop' || name === 'restart') &&
        !confirm(`${name} the workbench? Conversations in progress will end.`)) return;
    busy = name;
    try {
      await fn();
      await refresh();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = null;
    }
  }

  async function openWorkbench() {
    // Cross-origin by construction: this page is on the gateway front, the
    // workbench is on its own. A new tab, never an iframe.
    try {
      window.open(await api.signinUrl(), '_blank');
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
  }

  async function toggleLog() {
    showLog = !showLog;
    if (showLog) log = (await api.logs(200)).log;
  }

  onMount(() => {
    refresh();
    timer = setInterval(refresh, 10_000);
  });
  onDestroy(() => { if (timer) clearInterval(timer); });

  const daemon = $derived(st?.daemon ?? null);
  const up = $derived(!!daemon?.running);
  const uptime = $derived(
    daemon?.supervised_since
      ? `${Math.floor((Date.now() / 1000 - daemon.supervised_since) / 60)} min`
      : null,
  );
</script>

<main data-state={up ? 'up' : 'down'}>
  <header>
    <h1>Claude Science</h1>
    <span class="badge">{up ? 'running' : 'not running'}</span>
  </header>

  <button class="primary" onclick={openWorkbench} disabled={!up}>
    Open workbench
  </button>
  <p class="hint">
    Signs you in with the awm session you already hold. Opens in a new tab —
    the workbench has its own origin.
  </p>

  {#if error}
    <p class="error">{error}</p>
  {/if}

  {#if st}
    <section>
      <h2>Daemon</h2>
      <dl>
        <dt>version</dt><dd>{daemon?.version ?? '—'}</dd>
        <dt>pid</dt><dd>{daemon?.pid ?? '—'}</dd>
        <dt>port</dt><dd>{daemon?.port ?? '—'} <span class="dim">(preview {daemon?.sandbox_port ?? '—'})</span></dd>
        <dt>lifetime</dt>
        <dd>
          {#if daemon?.adopted === true}
            adopted — started before this service, and outlives it
          {:else if daemon?.adopted === false}
            started by this service{uptime ? `, ${uptime} ago` : ''}
          {:else}
            —
          {/if}
        </dd>
        {#if daemon?.error}<dt>error</dt><dd class="error">{daemon.error}</dd>{/if}
      </dl>
      <div class="row">
        <button onclick={() => act('start', api.start)} disabled={!!busy || up}>Start</button>
        <button onclick={() => act('restart', api.restart)} disabled={!!busy}>Restart</button>
        <button onclick={() => act('stop', api.stop)} disabled={!!busy || !up}>Stop</button>
        <button onclick={toggleLog} disabled={!!busy}>{showLog ? 'Hide log' : 'Log'}</button>
      </div>
      {#if showLog}<pre class="log">{log || '(empty)'}</pre>{/if}
    </section>

    <section>
      <h2>Install</h2>
      <ul class="steps">
        <li data-ok={st.install.binary.installed}>
          binary <span class="dim">{st.install.binary.path}</span>
        </li>
        <li data-ok={st.install.data_dir.provisioned}>
          data dir provisioned <span class="dim">{st.install.data_dir.path}</span>
        </li>
        <li data-ok={st.install.daemon.running}>daemon running</li>
      </ul>
    </section>

    <section>
      <h2>Mesh fronts</h2>
      <ul class="steps">
        {#each Object.entries(st.fronts) as [name, f]}
          <li data-ok={f.serving && f.tls}>
            {name} <span class="dim">:{f.listener_port} → {f.upstream}</span>
            {#if f.error}<span class="error"> {f.error}</span>{/if}
          </li>
        {/each}
      </ul>
      <p class="hint">Allowed origins: {st.origins.join(', ')}</p>
    </section>

    <section>
      <h2>awm connector</h2>
      <ul class="steps">
        <li data-ok={st.mcp_bridge.mounted}>
          mounted at <span class="dim">{st.mcp_bridge.prefix}</span>
        </li>
      </ul>
      <p class="hint">
        {st.mcp_bridge.tool_count} verbs allowlisted across
        {Object.keys(st.mcp_bridge.allowlist).length} domains:
        {Object.keys(st.mcp_bridge.allowlist).join(', ')}
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
  h1 { font-size: 1.4rem; margin: 0 0 1rem; }
  h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em;
       color: var(--text-dim, #9a9a9a); margin: 2rem 0 0.5rem; }
  .badge {
    font-size: 0.75rem; padding: 0.1rem 0.5rem; border-radius: 999px;
    border: 1px solid currentColor;
  }
  main[data-state='up'] .badge { color: var(--ok, #4ade80); }
  main[data-state='down'] .badge { color: var(--warn, #fbbf24); }

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

  dl { display: grid; grid-template-columns: 8rem 1fr; gap: 0.25rem 1rem; margin: 0; }
  dt { color: var(--text-dim, #9a9a9a); }
  dd { margin: 0; }

  .steps { list-style: none; padding: 0; margin: 0; }
  .steps li { padding-left: 1.4rem; position: relative; margin: 0.2rem 0; }
  /* The marker carries the state, so a screenshot of this page is readable
     without colour vision: ✓ / ✗, not two shades of dot. */
  .steps li::before { content: '✗'; position: absolute; left: 0; color: var(--warn, #fbbf24); }
  .steps li[data-ok='true']::before { content: '✓'; color: var(--ok, #4ade80); }

  .dim { color: var(--text-dim, #9a9a9a); }
  .hint { color: var(--text-dim, #9a9a9a); font-size: 0.85rem; }
  .error { color: var(--error, #f87171); }
  .log {
    max-height: 22rem; overflow: auto; font-size: 0.8rem;
    background: var(--surface, #1a1a1a); padding: 0.75rem; border-radius: 6px;
  }
</style>
