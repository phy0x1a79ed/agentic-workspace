<script lang="ts">
  // The dashboard's reception page: is it healthy, is it routable, and a way in.
  //
  // Two audiences, one screen. Most visits are "take me to the dashboard", so
  // the Open button is the first thing and everything else is beneath it. The
  // rest is for the visit where the button did *not* work — which is why the
  // dashboard process, the TLS front and hermes' own self-report are shown as
  // three separate states rather than one green dot. There are three ways this
  // can be down and they need three different fixes: the adopted process, the
  // TLS listener, and hermes' own internals.
  import { onMount, onDestroy } from 'svelte';
  import * as api from './lib/api';

  let st = $state<api.Status | null>(null);
  let error = $state<string | null>(null);
  let absent = $state(false);
  let busy = $state<string | null>(null);
  let log = $state<string>('');
  let showLog = $state(false);
  let timer: ReturnType<typeof setInterval> | null = null;

  async function refresh() {
    try {
      st = await api.status();
      error = null;
      absent = false;
    } catch (e) {
      // A profile-gated service that does not run here is a fact to state, not
      // a failure to retry against — say so once and stop pretending to load.
      if (api.isServiceAbsent(e)) {
        absent = true;
        error = null;
      } else {
        error = e instanceof Error ? e.message : String(e);
      }
    }
  }

  async function act(name: string, fn: () => Promise<unknown>) {
    if (busy) return;
    // stop/restart end live chat sessions and PTYs. Sessions are persisted, so
    // this costs a reconnect rather than a conversation — worth saying, not
    // worth a confirm dialog that trains people past it.
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

  function openDashboard() {
    // Cross-origin by construction: this page is on the gateway edge, the
    // dashboard on its own front. A new tab, never an iframe.
    if (st?.front.url) window.open(st.front.url, '_blank');
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

  const d = $derived(st?.dashboard ?? null);
  const front = $derived(st?.front ?? null);
  const health = $derived(st?.health ?? null);
  const model = $derived(st?.model ?? null);
  // Reachable means both: the dashboard bound its port, and the front in front
  // of it is serving. Either one missing and the button lies.
  const reachable = $derived(!!d?.listening && !!front?.serving);
  // A null unit means no user manager here, so the dashboard shares awm's
  // cgroup and dies with the next deploy. Visible now beats discovered later.
  const survivesDeploy = $derived(!!d?.user_unit?.available && !!d?.user_unit?.unit);
  // An adopted dashboard was already running when this service arrived, so its
  // start time is not ours to know — say "adopted" rather than dashing a number
  // we never had. Only a dashboard we launched has an uptime to report.
  const supervised = $derived(
    d?.supervised_since != null
      ? `${Math.floor((Date.now() / 1000 - d.supervised_since) / 60)} min`
      : d?.adopted
        ? 'adopted (already running)'
        : '—',
  );
</script>

<main data-state={reachable ? 'up' : 'down'}>
  <header>
    <h1>Hermes Agent</h1>
    <span class="badge">{reachable ? 'reachable' : 'not reachable'}</span>
  </header>

  {#if absent}
    <p class="warn">
      The hermes service is not running on this node. It is profile-gated —
      it bootstraps only where <code>AWM_PROFILES</code> names <code>hermes</code>
      — so this is the expected page on a node that does not host the dashboard.
    </p>
  {:else}
    <button class="primary" onclick={openDashboard} disabled={!reachable}>
      Open dashboard
    </button>
    <p class="hint">
      Behind the awm session you already hold. Opens in a new tab — the
      dashboard has its own origin{front?.url ? ` (${front.url})` : ''}, because
      its bundle resolves assets from the server root and cannot be served under
      a path prefix.
    </p>
  {/if}

  {#if error}
    <p class="error">{error}</p>
  {/if}

  {#if st && d}
    <section>
      <h2>Dashboard</h2>
      <dl>
        <dt>version</dt><dd>{health?.api_status?.version ?? '—'}</dd>
        <dt>pid</dt><dd>{d.pid ?? '—'}</dd>
        <dt>port</dt><dd>{d.port} <span class="dim">(loopback)</span></dd>
        <dt>supervised</dt><dd>{supervised}</dd>
        <dt>sessions</dt><dd>{health?.api_status?.active_sessions ?? '—'}</dd>
        <dt>home</dt><dd class="dim">{d.home}</dd>
      </dl>
      <ul class="steps">
        <li data-ok={d.installed}>launcher installed <span class="dim">{d.bin}</span></li>
        <li data-ok={d.web_dist}>SPA bundle built</li>
        <li data-ok={d.listening}>listening on :{d.port}</li>
        <li data-ok={survivesDeploy}>
          {survivesDeploy
            ? `outlives an awm restart (${d.user_unit.unit})`
            : 'NOT in a user unit — dies with the next awm restart'}
        </li>
      </ul>
      {#if health?.error}<p class="error">{health.error}</p>{/if}
      <div class="row">
        <button onclick={() => act('start', api.start)} disabled={!!busy || d.listening}>Start</button>
        <button onclick={() => act('restart', api.restart)} disabled={!!busy}>Restart</button>
        <button onclick={() => act('stop', api.stop)} disabled={!!busy || !d.listening}>Stop</button>
        <button onclick={toggleLog} disabled={!!busy}>{showLog ? 'Hide log' : 'Log'}</button>
      </div>
      <p class="hint">Restart and Stop end live chat sessions and PTYs.</p>
      {#if showLog}<pre class="log">{log || '(empty)'}</pre>{/if}
    </section>

    <section>
      <h2>Mesh front</h2>
      <ul class="steps">
        <li data-ok={front?.serving && front?.tls}>
          serving <span class="dim">:{front?.listener_port} → {front?.upstream}</span>
          {#if front?.error}<span class="error"> {front.error}</span>{/if}
        </li>
      </ul>
      <p class="hint">Certificate covers: {front?.san ?? '—'}</p>
    </section>

    <section>
      <h2>Model</h2>
      <dl>
        <dt>default</dt><dd>{model?.default ?? '—'}</dd>
        <dt>provider</dt><dd>{model?.provider ?? '—'}</dd>
        <dt>base url</dt><dd class="dim">{model?.base_url ?? '—'}</dd>
      </dl>
      {#if model?.error}<p class="error">{model.error}</p>{/if}
      <p class="hint">
        Read from hermes' own <code>config.yaml</code>. Changing it needs a
        dashboard restart before a running session sees it.
      </p>
    </section>
  {:else if !error && !absent}
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

  .steps { list-style: none; padding: 0; margin: 0.75rem 0 0; }
  .steps li { padding-left: 1.4rem; position: relative; margin: 0.2rem 0; }
  /* The marker carries the state, so a screenshot of this page is readable
     without colour vision: ✓ / ✗, not two shades of dot. */
  .steps li::before { content: '✗'; position: absolute; left: 0; color: var(--warn, #fbbf24); }
  .steps li[data-ok='true']::before { content: '✓'; color: var(--ok, #4ade80); }

  code { font-family: var(--font-mono, ui-monospace, monospace); font-size: 0.9em; }
  .dim { color: var(--text-dim, #9a9a9a); }
  .warn { color: var(--warn, #fbbf24); }
  .hint { color: var(--text-dim, #9a9a9a); font-size: 0.85rem; }
  .error { color: var(--error, #f87171); }
  .log {
    max-height: 22rem; overflow: auto; font-size: 0.8rem;
    background: var(--surface, #1a1a1a); padding: 0.75rem; border-radius: 6px;
  }
</style>
