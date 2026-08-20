<script lang="ts">
  // The harness's reception page: is it healthy, is it routable, and a way in.
  //
  // Two audiences, one screen. Most visits are "take me to the harness", so the
  // Open button is the first thing and everything else is beneath it. The rest
  // is for the visit where the button did *not* work — which is why the harness,
  // the front and the model route are shown as three separate states rather
  // than one green dot. There are three ways this can be down and they need
  // three different fixes: the node process, the TLS listener, and a provider
  // with no key behind it.
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
    // stop/restart drop live GUI connections. Sessions are persisted, so this
    // costs a reconnect rather than a conversation — worth saying, not worth a
    // confirm dialog that trains people past it.
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

  function openHarness() {
    // Cross-origin by construction: this page is on the gateway front, the
    // harness on its own. A new tab, never an iframe.
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

  const h = $derived(st?.harness ?? null);
  const front = $derived(st?.front ?? null);
  // Reachable means all three: the process is up, it bound its port, and the
  // front in front of it is serving. Any one missing and the button lies.
  const reachable = $derived(!!h?.listening && !!front?.serving);
  const route = $derived(h?.model_route ?? null);
  const routable = $derived(!!route?.provider_declared && !!route?.key_available);
  const uptime = $derived(
    h?.uptime_s != null ? `${Math.floor(h.uptime_s / 60)} min` : null,
  );
</script>

<main data-state={reachable ? 'up' : 'down'}>
  <header>
    <h1>DeepSeek Harness</h1>
    <span class="badge">{reachable ? 'reachable' : 'not reachable'}</span>
  </header>

  <button class="primary" onclick={openHarness} disabled={!reachable}>
    Open harness
  </button>
  <p class="hint">
    Behind the awm session you already hold. Opens in a new tab —
    the harness has its own origin{front?.url ? ` (${front.url})` : ''}.
  </p>

  {#if error}
    <p class="error">{error}</p>
  {/if}

  {#if !routable && route}
    <p class="warn">
      No usable model route: {route.provider_declared ? '' : 'no provider declared; '}
      {route.key_available ? '' : `no key at ${route.key_source}`}. The GUI will
      load with an empty model picker.
    </p>
  {/if}

  {#if st && h}
    <section>
      <h2>Harness</h2>
      <dl>
        <dt>version</dt><dd>{h.version ?? '—'}</dd>
        <dt>pid</dt><dd>{h.pid ?? '—'}</dd>
        <dt>port</dt><dd>{h.port} <span class="dim">(loopback)</span></dd>
        <dt>uptime</dt><dd>{uptime ?? '—'}</dd>
        <dt>workspace</dt><dd class="dim">{h.workdir}</dd>
        {#if h.error}<dt>error</dt><dd class="error">{h.error}</dd>{/if}
      </dl>
      <ul class="steps">
        <li data-ok={h.installed}>runtime installed <span class="dim">{h.runtime}</span></li>
        <li data-ok={h.running}>
          process running{h.exit_code != null && !h.running ? ` (last exit ${h.exit_code})` : ''}
        </li>
        <li data-ok={h.listening}>listening on :{h.port}</li>
      </ul>
      <div class="row">
        <button onclick={() => act('start', api.start)} disabled={!!busy || h.running}>Start</button>
        <button onclick={() => act('restart', api.restart)} disabled={!!busy}>Restart</button>
        <button onclick={() => act('stop', api.stop)} disabled={!!busy || !h.running}>Stop</button>
        <button onclick={toggleLog} disabled={!!busy}>{showLog ? 'Hide log' : 'Log'}</button>
      </div>
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
      <h2>Model route</h2>
      <ul class="steps">
        <li data-ok={route?.provider_declared}>
          provider declared <span class="dim">{route?.settings}</span>
        </li>
        <li data-ok={route?.key_available}>
          key available as <span class="dim">{route?.key_env}</span>
        </li>
      </ul>
      <p class="hint">
        The key is read from {route?.key_source} at launch and passed to the
        harness as an environment variable. It is never written to settings.
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

  .steps { list-style: none; padding: 0; margin: 0.75rem 0 0; }
  .steps li { padding-left: 1.4rem; position: relative; margin: 0.2rem 0; }
  /* The marker carries the state, so a screenshot of this page is readable
     without colour vision: ✓ / ✗, not two shades of dot. */
  .steps li::before { content: '✗'; position: absolute; left: 0; color: var(--warn, #fbbf24); }
  .steps li[data-ok='true']::before { content: '✓'; color: var(--ok, #4ade80); }

  .dim { color: var(--text-dim, #9a9a9a); }
  .warn { color: var(--warn, #fbbf24); }
  .hint { color: var(--text-dim, #9a9a9a); font-size: 0.85rem; }
  .error { color: var(--error, #f87171); }
  .log {
    max-height: 22rem; overflow: auto; font-size: 0.8rem;
    background: var(--surface, #1a1a1a); padding: 0.75rem; border-radius: 6px;
  }
</style>
