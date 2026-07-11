<script lang="ts">
  /**
   * Claude FleetView in the browser — a machine-wide roster of every agent,
   * grouped by status, with a full-screen terminal overlay to view/drive any
   * attachable one, plus spawn + dispose. Subsumes the attention board: the
   * needs-you set still drives desktop push.
   *
   * Data flow (mirrors the notifications board): one list_fleet fetch is the
   * source of truth; the notifications `feed` WS is a doorbell that debounce-
   * refetches; a slow reconcile poll backstops (60s live / 15s down).
   */
  import { onDestroy, onMount } from 'svelte';
  import { svc, toWsUrl } from '@awm/client';
  import { FleetTerminal } from '@awm/terminal';
  import { fetchFleet, killTmux, forgetSession, markSeen, type SpawnResult } from './lib/api';
  import {
    visibleColumns, cellText, sectionOf, labelFor,
    SECTION_ORDER, STATE_LABEL, STATE_ICON,
  } from './lib/columns';
  import type { FleetSession, FleetConfig } from './lib/types';
  import SpawnOverlay from './SpawnOverlay.svelte';
  import ConfigOverlay from './ConfigOverlay.svelte';

  const NOTIF = svc('notifications');
  const LIVE_RECONCILE_MS = 60_000;
  const DOWN_POLL_MS = 15_000;
  const TICK_MS = 5_000;

  let sessions = $state<FleetSession[]>([]);
  let config = $state<FleetConfig>({
    column_order: [], hidden_columns: [],
    spawn_defaults: { harness: 'claude', model: 'sonnet', effort: 'medium', scope: '' },
  });
  let now = $state(Date.now() / 1000);
  let wsLive = $state(false);
  let loadError = $state<string | null>(null);
  let search = $state('');
  let collapsed = $state<Set<string>>(new Set(['ended']));
  let notifPerm = $state<NotificationPermission>(
    'Notification' in window ? Notification.permission : 'denied',
  );

  // Overlays: one at a time.
  let overlay = $state<'none' | 'terminal' | 'spawn' | 'config'>('none');
  let selected = $state<FleetSession | null>(null);
  let disposeArmed = $state<string | null>(null);
  let disposeTimer: ReturnType<typeof setTimeout> | null = null;

  const columns = $derived(visibleColumns(config.column_order, config.hidden_columns));

  const filtered = $derived.by(() => {
    const q = search.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter((s) =>
      [labelFor(s), s.harness, s.model ?? '', s.cwd ?? '', s.state]
        .join(' ').toLowerCase().includes(q));
  });

  const grouped = $derived.by(() => {
    const g = new Map<string, FleetSession[]>();
    for (const s of filtered) {
      const sec = sectionOf(s.state);
      (g.get(sec) ?? g.set(sec, []).get(sec)!).push(s);
    }
    return SECTION_ORDER
      .map((sec) => [sec, g.get(sec) ?? []] as const)
      .filter(([, rows]) => rows.length > 0);
  });

  const counts = $derived({
    total: sessions.length,
    needsYou: sessions.filter((s) => sectionOf(s.state) === 'needs-you').length,
    attachable: sessions.filter((s) => s.attachable).length,
  });

  const cwdSuggestions = $derived(
    [...new Set(sessions.map((s) => s.cwd).filter((c): c is string => !!c))].sort());

  $effect(() => {
    document.title = counts.needsYou ? `(${counts.needsYou}) fleet` : 'fleet';
  });

  // ---- data ----------------------------------------------------------------

  let refetchTimer: ReturnType<typeof setTimeout> | null = null;

  async function refresh(): Promise<void> {
    try {
      const res = await fetchFleet();
      sessions = res.sessions;
      config = res.config;
      now = res.now;
      loadError = null;
      // Keep the open terminal's session object fresh (state/attention).
      if (selected) {
        selected = sessions.find((s) => s.session_id === selected!.session_id) ?? selected;
      }
      pushDue();
    } catch (e) {
      loadError = e instanceof Error ? e.message : String(e);
    }
  }

  function refreshSoon(): void {
    if (refetchTimer) return;
    refetchTimer = setTimeout(() => { refetchTimer = null; void refresh(); }, 250);
  }

  // ---- live feed WS --------------------------------------------------------

  let ws: WebSocket | null = null;
  let wsBackoff = 1_000;
  let closed = false;

  function connectFeed(): void {
    if (closed) return;
    try {
      ws = new WebSocket(toWsUrl(NOTIF.url('/emit/feed')));
    } catch { scheduleReconnect(); return; }
    ws.addEventListener('open', () => { wsLive = true; wsBackoff = 1_000; refreshSoon(); });
    ws.addEventListener('message', () => refreshSoon());
    ws.addEventListener('close', () => { wsLive = false; scheduleReconnect(); });
    ws.addEventListener('error', () => ws?.close());
  }
  function scheduleReconnect(): void {
    if (closed) return;
    setTimeout(connectFeed, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 2, 30_000);
  }

  // ---- desktop push (subsumes the attention board) -------------------------

  const pushed = new Set<string>();
  async function enableNotifications(): Promise<void> {
    if (!('Notification' in window)) return;
    notifPerm = await Notification.requestPermission();
    if (notifPerm === 'granted') pushDue();
  }
  function pushDue(): void {
    if (notifPerm !== 'granted') return;
    const t = Date.now() / 1000;
    for (const s of sessions) {
      for (const it of s.open_items) {
        if (it.kind !== 'needs-you' && it.kind !== 'error') continue;
        if (it.seen_at || pushed.has(it.id) || t < it.notify_at) continue;
        pushed.add(it.id);
        try {
          const n = new Notification(`${it.kind} — ${labelFor(s)}`, {
            body: it.snippet ?? it.title, tag: it.id,
          });
          n.onclick = () => window.focus();
        } catch { /* best-effort */ }
        void markSeen(it.id);
      }
    }
  }

  // ---- actions -------------------------------------------------------------

  function openTerminal(s: FleetSession): void {
    if (!s.attachable) return;
    selected = s;
    overlay = 'terminal';
  }

  function armDispose(sid: string): void {
    if (disposeArmed === sid) return;
    disposeArmed = sid;
    if (disposeTimer) clearTimeout(disposeTimer);
    disposeTimer = setTimeout(() => { disposeArmed = null; }, 3_000);
  }

  async function confirmDispose(s: FleetSession): Promise<void> {
    disposeArmed = null;
    if (disposeTimer) clearTimeout(disposeTimer);
    // Optimistically drop the row.
    sessions = sessions.filter((x) => x.session_id !== s.session_id);
    try {
      if (s.attachable && s.tmux_session) await killTmux(s.tmux_session);
      else await forgetSession(s.session_id);
    } catch { /* ignore; the reconcile poll re-syncs */ }
    void refresh();
  }

  function onDisposeClick(e: MouseEvent, s: FleetSession): void {
    e.stopPropagation();
    if (disposeArmed === s.session_id) void confirmDispose(s);
    else armDispose(s.session_id);
  }

  function toggleSection(sec: string): void {
    const next = new Set(collapsed);
    if (next.has(sec)) next.delete(sec); else next.add(sec);
    collapsed = next;
  }

  function onSpawned(r: SpawnResult): void {
    overlay = 'none';
    void refresh();
    // Attach the freshly-spawned session as soon as it shows in the roster.
    const attach = (tries: number): void => {
      const s = sessions.find((x) => x.tmux_session === r.tmux_session);
      if (s) { openTerminal(s); return; }
      if (tries > 0) setTimeout(() => { void refresh().then(() => attach(tries - 1)); }, 800);
    };
    setTimeout(() => attach(6), 800);
  }

  onMount(() => {
    void refresh();
    connectFeed();
    const tick = setInterval(() => { now = Date.now() / 1000; pushDue(); }, TICK_MS);
    const reconcile = setInterval(() => void refresh(), wsLive ? LIVE_RECONCILE_MS : DOWN_POLL_MS);
    return () => { clearInterval(tick); clearInterval(reconcile); };
  });
  onDestroy(() => {
    closed = true; ws?.close();
    if (refetchTimer) clearTimeout(refetchTimer);
    if (disposeTimer) clearTimeout(disposeTimer);
  });
</script>

<main class="fleet">
  <header class="bar mono">
    <span class="brand">fleet<span class="cursor" aria-hidden="true">▍</span></span>
    <span class="tallies">
      <span class="tally n" class:zero={counts.needsYou === 0}>need:{counts.needsYou}</span>
      <span class="tally">agents:{counts.total}</span>
    </span>
    <span class="spacer"></span>
    <input class="search" placeholder="filter…" bind:value={search} />
    <button class="icon" title="new agent" onclick={() => (overlay = 'spawn')}>＋</button>
    <button class="icon" title="columns" onclick={() => (overlay = 'config')}>⚙</button>
    <span class="conn" class:live={wsLive}>{wsLive ? '● live' : '○ polling'}</span>
  </header>

  {#if notifPerm === 'default'}
    <button class="notif-cta" onclick={() => void enableNotifications()}>
      Enable desktop notifications
      <span class="sub">needs-you pings land even when this tab is backgrounded</span>
    </button>
  {/if}
  {#if loadError}
    <p class="load-error mono">can't reach the fleet service: {loadError} — retrying</p>
  {/if}

  {#if sessions.length === 0 && !loadError}
    <div class="empty"><span class="bigcursor" aria-hidden="true">▍</span>
      <p>No agents seen yet.</p></div>
  {/if}

  {#each grouped as [sec, rows] (sec)}
    <section class="group">
      <button class="group-head mono state-{sec}" onclick={() => toggleSection(sec)}>
        <span class="dot">{STATE_ICON[sec]}</span>
        <span class="state-name">{STATE_LABEL[sec]}</span>
        <span class="group-count">{rows.length}</span>
        <span class="spacer"></span>
        <span class="chev">{collapsed.has(sec) ? '▸' : '▾'}</span>
      </button>

      {#if !collapsed.has(sec)}
        <div class="rows">
          {#each rows as s (s.session_id)}
            <div class="row" class:attachable={s.attachable}
                 onclick={() => openTerminal(s)} role="button" tabindex="0"
                 onkeydown={(e) => { if (e.key === 'Enter') openTerminal(s); }}>
              {#each columns as col (col.key)}
                {#if col.key === 'status'}
                  <span class="cell status state-{sectionOf(s.state)}">
                    <span class="s-dot">{STATE_ICON[sectionOf(s.state)]}</span>
                    <span class="s-word">{STATE_LABEL[sectionOf(s.state)]}</span>
                  </span>
                {:else if col.key === 'attachable'}
                  <span class="cell term" class:on={s.attachable}
                        title={s.attachable ? 'attach terminal' : 'not attachable'}>
                    {s.attachable ? '▸' : '—'}
                  </span>
                {:else if col.key === 'dispose'}
                  <button class="cell dispose" class:armed={disposeArmed === s.session_id}
                          onclick={(e) => onDisposeClick(e, s)}
                          title="dispose (tap twice)">
                    {disposeArmed === s.session_id ? 'sure?' : '✕'}
                  </button>
                {:else}
                  <span class="cell" class:numeric={col.numeric}
                        title={col.key === 'title' ? (s.cwd ?? '') : ''}>
                    {cellText(col.key, s, now)}
                  </span>
                {/if}
              {/each}
            </div>
          {/each}
        </div>
      {/if}
    </section>
  {/each}
</main>

{#if overlay === 'terminal' && selected}
  {#key selected.session_id}
    <FleetTerminal
      target={{ tmux_session: selected.tmux_session ?? undefined }}
      title={labelFor(selected)}
      subtitle={`${selected.harness}${selected.model ? ' · ' + selected.model.replace(/^claude-/, '') : ''}`}
      attachable={selected.attachable}
      onClose={() => { overlay = 'none'; selected = null; }}
    />
  {/key}
{/if}

{#if overlay === 'spawn'}
  <SpawnOverlay
    defaults={config.spawn_defaults}
    cwdSuggestions={cwdSuggestions}
    onClose={() => (overlay = 'none')}
    onSpawned={onSpawned}
  />
{/if}

{#if overlay === 'config'}
  <ConfigOverlay
    order={config.column_order}
    hidden={config.hidden_columns}
    onClose={() => (overlay = 'none')}
    onSaved={(order, hidden) => {
      config = { ...config, column_order: order, hidden_columns: hidden };
      overlay = 'none';
    }}
  />
{/if}

<style>
  .fleet {
    max-width: 860px; margin: 0 auto;
    padding: var(--space-5) var(--space-4) 64px;
    display: flex; flex-direction: column; gap: var(--space-4);
    min-height: 100vh;
  }
  .spacer { flex: 1; }

  .bar {
    display: flex; align-items: center; gap: var(--space-3);
    font-size: 12px; color: var(--text2);
    border-bottom: 1px solid var(--border);
    padding-bottom: var(--space-3);
    position: sticky; top: 0; z-index: 4;
    background: var(--bg, #0d0d0d);
  }
  .brand { color: var(--text); font-size: 14px; letter-spacing: 0.04em; }
  .cursor { display: inline-block; margin-left: 2px; color: var(--atomizer, #4b8bff);
    animation: blink 1.2s steps(1) infinite; }
  .tallies { display: flex; gap: var(--space-3); }
  .tally.n { color: var(--atomizer, #4b8bff); }
  .tally.zero { color: var(--text3); }
  .search {
    width: 30vw; max-width: 180px;
    padding: 5px 10px; font-size: 13px;
    background: var(--surface, #141414);
    border: 1px solid var(--border, #333);
    border-radius: 16px; color: var(--text, #eee);
  }
  .icon {
    background: var(--surface, #141414); border: 1px solid var(--border, #333);
    color: var(--text, #ddd); width: 30px; height: 30px; border-radius: 8px;
    cursor: pointer; font-size: 15px; line-height: 1;
  }
  .icon:hover { border-color: var(--atomizer, #4b8bff); }
  .conn { color: var(--text3); font-size: 11px; }
  .conn.live { color: var(--ok, #98c379); }

  .notif-cta {
    display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius-md); color: var(--text);
    font-size: 13px; padding: var(--space-3) var(--space-4); cursor: pointer; text-align: left;
  }
  .notif-cta .sub { color: var(--text3); font-size: 11px; }
  .load-error { font-size: 11px; color: var(--warn, #e5c07b); }

  .empty { display: flex; flex-direction: column; align-items: center; gap: var(--space-4);
    padding: 84px 0; color: var(--text2); font-size: 13px; }
  .bigcursor { font-size: 44px; color: var(--text3); animation: breathe 3.2s ease-in-out infinite; }

  /* -- groups --------------------------------------------------------------- */
  .group { display: flex; flex-direction: column; }
  .group-head {
    display: flex; align-items: center; gap: var(--space-3);
    font-size: 12px; padding: var(--space-3) var(--space-2);
    background: none; border: none; border-bottom: 1px solid var(--border);
    color: var(--text2); cursor: pointer; width: 100%; text-align: left;
  }
  .group-head .dot { font-size: 10px; }
  .group-head .state-name { font-weight: 600; letter-spacing: 0.03em; }
  .group-count { color: var(--text3); }
  .group-head.state-needs-you { color: var(--atomizer, #4b8bff); }
  .group-head.state-error { color: var(--danger, #e06c75); }
  .group-head.state-working { color: var(--ok, #98c379); }
  .chev { color: var(--text3); }

  .rows { display: flex; flex-direction: column; }
  .row {
    display: flex; align-items: center; gap: var(--space-3);
    padding: 9px var(--space-2);
    border-bottom: 1px solid var(--border);
    cursor: default; font-size: 13px;
  }
  .row.attachable { cursor: pointer; }
  .row.attachable:hover { background: var(--surface, #161616); }
  .cell { flex: 1 1 auto; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cell.numeric { flex: 0 0 auto; text-align: right; font-variant-numeric: tabular-nums;
    color: var(--text2); font-size: 12px; min-width: 44px; }

  .status { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 5px; min-width: 92px; }
  .status .s-dot { font-size: 9px; }
  .status.state-needs-you { color: var(--atomizer, #4b8bff); }
  .status.state-error { color: var(--danger, #e06c75); }
  .status.state-working { color: var(--ok, #98c379); }
  .status.state-idle { color: var(--text2); }
  .status.state-ended { color: var(--text3); }

  .term { flex: 0 0 auto; width: 28px; text-align: center; color: var(--text3); }
  .term.on { color: var(--atomizer, #4b8bff); }

  .dispose {
    flex: 0 0 auto; min-width: 30px; height: 26px; padding: 0 8px;
    background: none; border: 1px solid transparent; border-radius: 6px;
    color: var(--text3); cursor: pointer; font-size: 12px;
  }
  .dispose:hover { color: var(--danger, #e06c75); }
  .dispose.armed {
    color: #fff; background: var(--danger, #e06c75);
    border-color: var(--danger, #e06c75); font-weight: 600;
  }

  button:focus-visible { outline: 1px solid var(--atomizer, #4b8bff); outline-offset: 2px; }
  @keyframes blink { 0%, 55% { opacity: 1; } 56%, 100% { opacity: 0; } }
  @keyframes breathe { 0%, 100% { opacity: 0.35; } 50% { opacity: 0.9; } }
  @media (prefers-reduced-motion: reduce) { .cursor, .bigcursor { animation: none !important; } }

  @media (max-width: 560px) {
    .fleet { padding: var(--space-4) var(--space-3) 48px; }
    .cell.numeric { min-width: 36px; }
    .status { min-width: 74px; }
    .search { width: 24vw; }
  }
</style>
