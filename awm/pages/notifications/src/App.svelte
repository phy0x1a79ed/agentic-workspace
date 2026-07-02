<script lang="ts">
  /**
   * Attention board — "who needs me right now?" in under a second.
   *
   * Data flow:
   *   · one `list` fetch is the source of truth (items + sessions);
   *   · the `feed` emitter WS (/svc/notifications/emit/feed) is a doorbell —
   *     every frame debounce-refetches `list`. The WS is load-bearing, not a
   *     nicety: hidden tabs throttle timers to ~1/min, but WS messages still
   *     fire, so desktop pushes stay prompt while the tab is backgrounded;
   *   · a slow reconcile poll (60s live / 15s while the WS is down) backstops.
   *
   * Desktop pushes honour the service's notify_at gate: question/error push on
   * arrival, idle only after its grace period survives unresolved. seen_at is
   * the cross-tab dedupe — we mark_seen as we push.
   */
  import { onDestroy, onMount } from 'svelte';
  import { svc, toWsUrl } from '@awm/client';

  type Kind = 'question' | 'idle' | 'error';
  interface Item {
    id: string;
    session_id: string;
    kind: Kind;
    title: string;
    detail: string | null;
    snippet: string | null;
    created_at: number;
    notify_at: number;
    seen_at: number | null;
    resolved_at: number | null;
    resolved_by: string | null;
  }
  interface Session {
    session_id: string;
    harness: string;
    cwd: string | null;
    project: string | null;
    title: string | null;
    state: string;
    last_seen: number;
  }

  const NS = svc('notifications');
  const LIVE_RECONCILE_MS = 60_000;
  const DOWN_POLL_MS = 15_000;
  const TICK_MS = 5_000;

  let items = $state<Item[]>([]);
  let sessions = $state<Record<string, Session>>({});
  let now = $state(Date.now() / 1000);
  let wsLive = $state(false);
  let loadError = $state<string | null>(null);
  let notifPerm = $state<NotificationPermission>(
    'Notification' in window ? Notification.permission : 'denied',
  );

  const open = $derived(items.filter((i) => !i.resolved_at));
  const recent = $derived(items.filter((i) => i.resolved_at));
  const counts = $derived({
    question: open.filter((i) => i.kind === 'question').length,
    idle: open.filter((i) => i.kind === 'idle').length,
    error: open.filter((i) => i.kind === 'error').length,
  });
  const bySession = $derived.by(() => {
    const groups = new Map<string, Item[]>();
    for (const it of open) {
      const g = groups.get(it.session_id) ?? [];
      g.push(it);
      groups.set(it.session_id, g);
    }
    // Most-urgent session first: error > question > idle, then newest.
    const rank: Record<Kind, number> = { error: 0, question: 1, idle: 2 };
    return [...groups.entries()].sort(([, a], [, b]) => {
      const ra = Math.min(...a.map((i) => rank[i.kind]));
      const rb = Math.min(...b.map((i) => rank[i.kind]));
      if (ra !== rb) return ra - rb;
      return Math.max(...b.map((i) => i.created_at)) - Math.max(...a.map((i) => i.created_at));
    });
  });

  $effect(() => {
    document.title = open.length ? `(${open.length}) attention` : 'attention';
  });

  // ---- data ---------------------------------------------------------------

  let refetchTimer: ReturnType<typeof setTimeout> | null = null;

  async function refresh(): Promise<void> {
    try {
      const res = await NS.fn<{ items: Item[]; sessions: Record<string, Session>; now: number }>(
        'list',
      );
      items = res.items;
      sessions = res.sessions;
      now = res.now;
      loadError = null;
      pushDue();
    } catch (e) {
      loadError = e instanceof Error ? e.message : String(e);
    }
  }

  function refreshSoon(): void {
    if (refetchTimer) return;
    refetchTimer = setTimeout(() => {
      refetchTimer = null;
      void refresh();
    }, 250);
  }

  // ---- live feed WS with reconnect -----------------------------------------

  let ws: WebSocket | null = null;
  let wsBackoff = 1_000;
  let closed = false;

  function connectFeed(): void {
    if (closed) return;
    try {
      ws = new WebSocket(toWsUrl(NS.url('/emit/feed')));
    } catch {
      scheduleReconnect();
      return;
    }
    ws.addEventListener('open', () => {
      wsLive = true;
      wsBackoff = 1_000;
      refreshSoon(); // catch anything missed while down
    });
    ws.addEventListener('message', () => refreshSoon());
    ws.addEventListener('close', () => {
      wsLive = false;
      scheduleReconnect();
    });
    ws.addEventListener('error', () => ws?.close());
  }

  function scheduleReconnect(): void {
    if (closed) return;
    setTimeout(connectFeed, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 2, 30_000);
  }

  // ---- desktop notifications ------------------------------------------------

  async function enableNotifications(): Promise<void> {
    if (!('Notification' in window)) return;
    notifPerm = await Notification.requestPermission();
    if (notifPerm === 'granted') pushDue();
  }

  const pushed = new Set<string>();

  function pushDue(): void {
    if (notifPerm !== 'granted') return;
    const t = Date.now() / 1000;
    for (const it of open) {
      // seen_at is the cross-tab gate; `pushed` the in-tab one.
      if (it.seen_at || pushed.has(it.id) || t < it.notify_at) continue;
      pushed.add(it.id);
      const sess = sessions[it.session_id];
      const where = sess?.project ?? sess?.cwd ?? it.session_id.slice(0, 8);
      try {
        const n = new Notification(`${label(it.kind)} — ${where}`, {
          body: it.snippet ?? it.title,
          tag: it.id,
        });
        n.onclick = () => window.focus();
      } catch {
        /* pushing is best-effort */
      }
      void NS.fn('mark_seen', { id: it.id }).catch(() => {});
    }
  }

  // ---- actions ---------------------------------------------------------------

  async function dismiss(id: string): Promise<void> {
    items = items.map((i) => (i.id === id ? { ...i, resolved_at: now, resolved_by: 'page' } : i));
    await NS.fn('resolve', { id }).catch(() => {});
    void refresh();
  }

  async function clearAll(): Promise<void> {
    items = items.map((i) => (i.resolved_at ? i : { ...i, resolved_at: now, resolved_by: 'page' }));
    await NS.fn('clear').catch(() => {});
    void refresh();
  }

  // ---- presentation -----------------------------------------------------------

  function label(kind: Kind): string {
    return kind === 'question' ? 'question' : kind === 'idle' ? 'idle' : 'error';
  }

  function age(ts: number): string {
    const s = Math.max(0, Math.floor(now - ts));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    return `${Math.floor(s / 86400)}d`;
  }

  function sessionLabel(id: string): string {
    const s = sessions[id];
    return s?.project ?? s?.cwd ?? id.slice(0, 8);
  }

  function sessionHarness(id: string): string {
    return sessions[id]?.harness ?? '';
  }

  onMount(() => {
    void refresh();
    connectFeed();
    const tick = setInterval(() => {
      now = Date.now() / 1000;
      pushDue(); // idle items crossing notify_at
    }, TICK_MS);
    const reconcile = setInterval(
      () => void refresh(),
      wsLive ? LIVE_RECONCILE_MS : DOWN_POLL_MS,
    );
    return () => {
      clearInterval(tick);
      clearInterval(reconcile);
    };
  });

  onDestroy(() => {
    closed = true;
    ws?.close();
    if (refetchTimer) clearTimeout(refetchTimer);
  });
</script>

<main class="board">
  <header class="statusline mono">
    <span class="brand">attention<span class="cursor" aria-hidden="true">▍</span></span>
    <span class="tallies">
      <span class="tally q" class:zero={counts.question === 0}>q:{counts.question}</span>
      <span class="tally i" class:zero={counts.idle === 0}>i:{counts.idle}</span>
      <span class="tally e" class:zero={counts.error === 0}>e:{counts.error}</span>
    </span>
    <span class="spacer"></span>
    {#if open.length > 0}
      <button class="linkish" onclick={() => void clearAll()}>clear all</button>
    {/if}
    <span class="conn" class:live={wsLive}>{wsLive ? '● live' : '○ polling'}</span>
  </header>

  {#if notifPerm === 'default'}
    <button class="notif-cta" onclick={() => void enableNotifications()}>
      Enable desktop notifications
      <span class="sub">pushes land even when this tab is in the background</span>
    </button>
  {:else if notifPerm === 'denied'}
    <p class="notif-denied mono">desktop notifications blocked in the browser — the board still updates live</p>
  {/if}

  {#if loadError}
    <p class="load-error mono">can't reach the notifications service: {loadError} — retrying</p>
  {/if}

  {#if open.length === 0}
    <div class="allclear">
      <span class="bigcursor" aria-hidden="true">▍</span>
      <p>All agents working — nothing needs you.</p>
    </div>
  {:else}
    <section class="groups">
      {#each bySession as [sid, group] (sid)}
        <article class="session">
          <header class="session-head mono">
            <span class="session-name" title={sessions[sid]?.cwd ?? sid}>{sessionLabel(sid)}</span>
            <span class="harness">{sessionHarness(sid)}</span>
            <span class="spacer"></span>
            <span class="session-age">{age(Math.max(...group.map((i) => i.created_at)))}</span>
          </header>
          {#each group as it (it.id)}
            <div class="item kind-{it.kind}">
              <span class="rail" aria-hidden="true"></span>
              <span class="kind mono">{label(it.kind)}</span>
              <span class="snippet" title={it.detail ?? undefined}>{it.snippet ?? it.title}</span>
              <button class="dismiss mono" title="Dismiss" onclick={() => void dismiss(it.id)}>✕</button>
            </div>
          {/each}
        </article>
      {/each}
    </section>
  {/if}

  {#if recent.length > 0}
    <section class="recent">
      <h2 class="mono">recent</h2>
      {#each recent.slice(0, 12) as it (it.id)}
        <div class="recent-row mono">
          <span class="kind kind-{it.kind}">{label(it.kind)}</span>
          <span class="who">{sessionLabel(it.session_id)}</span>
          <span class="what">{it.snippet ?? it.title}</span>
          <span class="spacer"></span>
          <span class="by">{it.resolved_by === 'user_response' ? 'answered' : it.resolved_by}</span>
        </div>
      {/each}
    </section>
  {/if}
</main>

<style>
  .board {
    max-width: 720px;
    margin: 0 auto;
    padding: var(--space-6) var(--space-5) 64px;
    display: flex;
    flex-direction: column;
    gap: var(--space-5);
    min-height: 100vh;
  }

  .spacer { flex: 1; }

  /* -- statusline ---------------------------------------------------------- */
  .statusline {
    display: flex;
    align-items: baseline;
    gap: var(--space-5);
    font-size: 11px;
    color: var(--text2);
    border-bottom: 1px solid var(--border);
    padding-bottom: var(--space-4);
  }
  .brand { color: var(--text); font-size: 13px; letter-spacing: 0.04em; }
  .cursor {
    display: inline-block;
    margin-left: 2px;
    color: var(--atomizer);
    animation: blink 1.2s steps(1) infinite;
  }
  .tallies { display: flex; gap: var(--space-3); }
  .tally.q { color: var(--atomizer); }
  .tally.i { color: var(--text2); }
  .tally.e { color: var(--danger); }
  .tally.zero { color: var(--text3); }
  .conn { color: var(--text3); }
  .conn.live { color: var(--ok); }
  .linkish {
    background: none;
    border: none;
    color: var(--text3);
    font: inherit;
    cursor: pointer;
    padding: 0;
  }
  .linkish:hover { color: var(--text2); }

  /* -- notification permission --------------------------------------------- */
  .notif-cta {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-1);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    color: var(--text);
    font-family: var(--sans);
    font-size: 13px;
    padding: var(--space-4) var(--space-5);
    cursor: pointer;
    text-align: left;
    transition: border-color 0.15s var(--ease-mech);
  }
  .notif-cta:hover { border-color: var(--atomizer); }
  .notif-cta .sub { color: var(--text3); font-size: 11px; }
  .notif-denied, .load-error { font-size: 11px; color: var(--text3); }
  .load-error { color: var(--warn); }

  /* -- all clear (the resting hero) ------------------------------------------ */
  .allclear {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-5);
    padding: 96px 0 64px;
    color: var(--text2);
    font-size: 13px;
  }
  .bigcursor {
    font-size: 48px;
    line-height: 1;
    color: var(--text3);
    animation: breathe 3.2s var(--ease-mech) infinite;
  }

  /* -- session groups --------------------------------------------------------- */
  .groups { display: flex; flex-direction: column; gap: var(--space-5); }
  .session {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }
  .session-head {
    display: flex;
    align-items: baseline;
    gap: var(--space-4);
    font-size: 11px;
    padding: var(--space-4) var(--space-5);
    border-bottom: 1px solid var(--border);
    color: var(--text);
  }
  .session-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .harness { color: var(--text3); }
  .session-age { color: var(--text3); }

  .item {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-4) var(--space-5) var(--space-4) 0;
    position: relative;
  }
  .item + .item { border-top: 1px solid var(--border); }
  .rail { align-self: stretch; width: 3px; flex: none; }
  .kind-question .rail { background: var(--atomizer); animation: pulse 2.4s var(--ease-mech) infinite; }
  .kind-error .rail { background: var(--danger); animation: pulse 1.6s var(--ease-mech) infinite; }
  .kind-idle .rail { background: var(--border2); }
  .item .kind { font-size: 11px; flex: none; width: 62px; }
  .kind-question .kind { color: var(--atomizer); }
  .kind-error .kind { color: var(--danger); }
  .kind-idle .kind { color: var(--text2); }
  .snippet {
    flex: 1;
    font-size: 13px;
    color: var(--text);
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  .dismiss {
    background: none;
    border: none;
    color: var(--text3);
    cursor: pointer;
    font-size: 11px;
    padding: var(--space-2);
    flex: none;
  }
  .dismiss:hover { color: var(--danger); }

  /* -- recent strip ------------------------------------------------------------ */
  .recent {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    opacity: 0.55;
    font-size: 11px;
  }
  .recent h2 {
    font-size: 11px;
    font-weight: 400;
    color: var(--text3);
    letter-spacing: 0.08em;
    margin-bottom: var(--space-2);
  }
  .recent-row { display: flex; gap: var(--space-4); align-items: baseline; }
  .recent-row .kind { width: 62px; flex: none; }
  .recent-row .kind.kind-question { color: var(--atomizer); }
  .recent-row .kind.kind-error { color: var(--danger); }
  .recent-row .kind.kind-idle { color: var(--text2); }
  .recent-row .who { color: var(--text2); flex: none; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .recent-row .what { color: var(--text3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .recent-row .by { color: var(--text3); flex: none; }

  button:focus-visible {
    outline: 1px solid var(--atomizer);
    outline-offset: 2px;
  }

  @keyframes blink { 0%, 55% { opacity: 1; } 56%, 100% { opacity: 0; } }
  @keyframes breathe { 0%, 100% { opacity: 0.35; } 50% { opacity: 0.9; } }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }

  @media (prefers-reduced-motion: reduce) {
    .cursor, .bigcursor, .rail { animation: none !important; }
  }

  @media (max-width: 560px) {
    .board { padding: var(--space-5) var(--space-4) 48px; }
    .item .kind { width: 48px; }
    .recent-row .who { max-width: 110px; }
  }
</style>
