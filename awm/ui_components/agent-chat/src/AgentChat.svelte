<script lang="ts">
  /**
   * Human ↔ live-agent chat for a DAG placement, fully self-contained.
   *
   * Composes:
   *   - a single unit-slug identity bar (this component);
   *   - <SttComposer> from @awm/stt-composer — text + mic (STT) input;
   *   - <TtsHistory> from @awm/tts-history — the transcript + per-row TTS replay
   *     + a Terminal tab onto the agent's live tmux pane.
   *
   * Identity is ONE unit slug — the DAG decouple retired the old (project, scope)
   * pair: an agent is a task-bound placement running in a workspace unit, keyed
   * by its globally-unique slug. The outer UI picks which agent to view (a task
   * uuid → its workspace_slug); this component just receives the slug (prop /
   * `?unit=` / `?slug=` / a `?task=` uuid it resolves via the DAG / localStorage).
   *
   * One transcript, one source of truth: on connect it live-subscribes to the
   * agents service transcript (backfill + push, deduped by act id, partials
   * coalesced per turn). Human input is spliced into the placement's stdin via
   * the agents service (`enqueue_post`) — NOT a scopes channel (a placement has
   * none) — and the agents service records that input back into the SAME
   * transcript, so the human turn renders from the stream (no optimistic echo).
   * Finalized agent messages are auto-spoken.
   *
   * Attached affordances: opening the transcript WS passively attaches the human
   * to the placement, which gates the DAG control actions — create a node at the
   * root (a fresh attended agent, jumped-to here) and relocate this node onto
   * another consumer.
   */
  import { onDestroy, untrack } from 'svelte';
  import {
    enqueueAgentPost,
    subscribeAgent,
    fetchDag,
    openNode,
    relocateSelf,
    type AgentSubscription,
    type AgentStreamEvent,
  } from '@awm/client';
  import { Chat, playOnce, type Post } from '@awm/chat';
  import { TranscriptFold, agentAuthor } from './agent-acts';

  interface Props {
    /** Initial unit slug (overrides ?unit= / ?slug= / ?task= / localStorage). */
    slug?: string;
    /** Auto-connect on mount when a slug resolves. Default true. */
    autoConnect?: boolean;
    /** Fully offline (gallery/demo): never connect, and put the embedded
     *  SttComposer in mock mode so it opens no /svc/stt session. Default false. */
    offline?: boolean;
  }
  let {
    slug: initSlug,
    autoConnect = true,
    offline = false,
  }: Props = $props();

  // --- Identity (URL ?unit=/?slug=/?task= → localStorage fallback) ----------

  const KEY_SLUG = 'awm.agent.slug';

  // One-time seed precedence: explicit prop → URL ?unit=/?slug= → localStorage.
  // A `?task=` uuid is resolved asynchronously (uuid → workspace_slug) below.
  // The prop is read non-reactively (untrack) — it only seeds the editable
  // field; after mount the identity bar owns `slug`.
  function initialIdentity(): { slug: string; task: string } {
    if (typeof window === 'undefined') return { slug: initSlug ?? '', task: '' };
    const params = new URLSearchParams(window.location.search);
    const slug =
      initSlug ??
      params.get('unit') ??
      params.get('slug') ??
      localStorage.getItem(KEY_SLUG) ??
      '';
    return { slug, task: params.get('task') ?? '' };
  }

  const seed = untrack(initialIdentity);
  let slug = $state(seed.slug);
  const taskParam = seed.task;

  // Identity of the *connected* placement (frozen at connect time so editing the
  // bar mid-session doesn't desync the live subscription).
  let liveSlug = $state('');
  let connected = $derived(!!liveSlug);

  // --- Transcript + auto-speak --------------------------------------------

  let posts = $state<Post[]>([]);
  let error = $state<string | null>(null);
  let status = $state<'idle' | 'connecting' | 'live'>('idle');
  let autoSpeak = $state(true);

  let fold: TranscriptFold | null = null;
  let sub: AgentSubscription | null = null;

  // Speak de-dupe so a re-render / reconnect can't double-play one message.
  let spokenIds = new Set<string>();

  // The transcript is the single source of truth — the fold owns every row
  // (agent acts AND the human turns the agents service records back via
  // record_in), so a rebuild just snapshots the fold.
  function rebuild() {
    posts = fold ? [...fold.posts] : [];
  }

  async function speak(p: Post): Promise<void> {
    try {
      await playOnce(p.body ?? '', '', {});
    } catch (err) {
      console.warn('tts playback failed', err);
    }
  }

  function maybeAutoSpeak(spoken: Post | null) {
    if (!spoken || !autoSpeak) return;
    const k = String(spoken.id ?? `${spoken.ts}-${spoken.author}`);
    if (spokenIds.has(k)) return;
    spokenIds.add(k);
    void speak(spoken);
  }

  // --- Live agent stream ---------------------------------------------------

  function onAgentEvent(ev: AgentStreamEvent) {
    if (!fold) return;
    if (ev.type === 'backfill') {
      // Seed history silently — don't auto-speak the whole transcript on connect.
      for (const act of ev.acts) fold.push(act);
      rebuild();
    } else if (ev.type === 'act') {
      const spoken = fold.push(ev.act);
      rebuild();
      maybeAutoSpeak(spoken);
    } else if (ev.type === 'lagged') {
      // Server dropped us behind a backpressure window and closed the socket.
      // Reconnect from the last act we folded so we don't miss the tail.
      reconnectAgent();
    } else if (ev.type === 'error') {
      error = ev.message;
    }
  }

  function openAgentStream(s: string) {
    sub?.close();
    const cursor = fold ? { after_ts: fold.lastTs, after_id: fold.lastId } : {};
    sub = subscribeAgent(s, cursor, onAgentEvent);
  }

  function reconnectAgent() {
    if (!connected) return;
    openAgentStream(liveSlug);
  }

  // --- Connect / disconnect ------------------------------------------------

  async function connect() {
    const s = slug.trim();
    if (!s) {
      error = 'a unit slug is required';
      return;
    }
    disconnect();
    error = null;
    status = 'connecting';
    liveSlug = s;
    if (typeof window !== 'undefined') localStorage.setItem(KEY_SLUG, s);

    fold = new TranscriptFold(agentAuthor(s));
    spokenIds = new Set();
    posts = [];

    // Live agent stream (backfill + push). The opening backfill frame replays
    // the transcript from the (empty) cursor — including any human turns the
    // agents service has recorded — so no separate backlog fetch is needed.
    // Opening this WS also passively attaches the human to the placement, which
    // is the precondition for the relocate control.
    openAgentStream(s);
    status = 'live';
    rebuild();
  }

  function disconnect() {
    sub?.close();
    sub = null;
    fold = null;
    liveSlug = '';
    status = 'idle';
  }

  // --- Human input → placement stdin (agents service) ----------------------

  let sending = $state(false);

  async function send(text: string) {
    const t = text.trim();
    if (!t || !connected || sending) return;
    sending = true;
    try {
      const enqueued = await enqueueAgentPost(liveSlug, t);
      if (!enqueued) {
        // No live session holds this slug (the placement isn't running). The
        // message wasn't delivered — surface it rather than silently dropping.
        error = 'no live agent holds this slug — message not delivered';
      } else {
        error = null;
      }
      // The delivered message returns through the transcript (record_in), so we
      // don't echo it locally — that keeps the transcript the single source of
      // truth and avoids a double-render.
    } catch (err) {
      error = `send failed: ${(err as Error).message}`;
    } finally {
      sending = false;
    }
  }

  // --- DAG controls: create node at root + relocate ------------------------

  let showNew = $state(false);
  let newGoal = $state('');
  let busyNew = $state(false);

  let showRelocate = $state(false);
  let relocateTarget = $state('');
  let busyRelocate = $state(false);

  async function createNode(e?: Event) {
    e?.preventDefault();
    const goal = newGoal.trim();
    if (!goal || busyNew) return;
    busyNew = true;
    error = null;
    try {
      // Open a node at the root (no consumer) — the orchestrator places an
      // attended worker on it and returns its workspace_slug, which we jump to.
      const res = await openNode(goal);
      newGoal = '';
      showNew = false;
      if (res?.workspace_slug) {
        slug = res.workspace_slug;
        await connect();
      } else {
        error = 'node opened but no workspace_slug returned';
      }
    } catch (err) {
      error = `create node failed: ${(err as Error).message}`;
    } finally {
      busyNew = false;
    }
  }

  async function relocate(e?: Event) {
    e?.preventDefault();
    if (!connected || busyRelocate) return;
    busyRelocate = true;
    error = null;
    try {
      // Act AS the attached placement (relocateSelf sends X-Awm-As: <slug>).
      // Empty target = relocate onto the global root.
      await relocateSelf(liveSlug, relocateTarget.trim() || undefined);
      relocateTarget = '';
      showRelocate = false;
    } catch (err) {
      error = `relocate failed: ${(err as Error).message}`;
    } finally {
      busyRelocate = false;
    }
  }

  // Recent transcript fed to the convo cleanup LLM as context (capped on the
  // composer side). Surfacing the last handful of turns keeps the STT cleanup
  // grounded in the live conversation.
  const chatContext = $derived(
    posts.slice(-20).map((p) => `${p.author}: ${p.body}`).join('\n'),
  );

  // --- Lifecycle -----------------------------------------------------------

  // Resolve a `?task=<uuid>` entry to its workspace_slug before first connect.
  // The outer UI hands the chat a task uuid; map it to the unit slug (the agent
  // identity) via the DAG read. Best-effort — a bad uuid leaves the bar empty.
  async function resolveTaskParam(): Promise<void> {
    if (!taskParam || slug.trim()) return;
    try {
      const dag = await fetchDag();
      const t = dag.tasks.find((x) => x.task_id === taskParam);
      if (t?.workspace_slug) slug = t.workspace_slug;
    } catch (err) {
      console.warn('task→slug resolution failed', err);
    }
  }

  let bootstrapped = false;
  $effect(() => {
    if (offline || !autoConnect || bootstrapped) return;
    bootstrapped = true;
    void (async () => {
      await resolveTaskParam();
      if (slug.trim() && status === 'idle') void connect();
    })();
  });

  onDestroy(() => disconnect());
</script>

<div class="agent-chat-root" data-awm-component="AgentChat">
  <!-- Pass the CONNECTED slug (frozen at connect time) so the Terminal tab
       attaches to the live agent; empty until connected → transcript only. -->
  <Chat
    {posts}
    onSend={send}
    {chatContext}
    onReplay={speak}
    {offline}
    slug={liveSlug}
  >
    {#snippet header()}
      <header class="hdr">
        <form
          class="connect-bar"
          onsubmit={(e) => {
            e.preventDefault();
            void connect();
          }}
        >
          <input
            class="field mono"
            type="text"
            placeholder="unit slug"
            bind:value={slug}
            aria-label="unit slug"
          />
          <button class="connect-btn mono" type="submit">
            {connected ? 'reconnect' : 'connect'}
          </button>
          <span class="state mono" data-status={status}>{status}</span>
        </form>
        <div class="hdr-actions">
          <button
            class="ctl mono"
            type="button"
            title="create a new node at the root"
            onclick={() => { showNew = !showNew; showRelocate = false; }}
          >＋ node</button>
          <button
            class="ctl mono"
            type="button"
            title={connected ? 'relocate this node' : 'attach first to relocate'}
            disabled={!connected}
            onclick={() => { showRelocate = !showRelocate; showNew = false; }}
          >relocate</button>
          <label class="speak-toggle mono">
            <input type="checkbox" bind:checked={autoSpeak} /> auto-speak
          </label>
        </div>
      </header>

      {#if showNew}
        <form class="mini-form mono" onsubmit={createNode}>
          <input
            class="field mono"
            type="text"
            placeholder="goal for the new root node"
            bind:value={newGoal}
            aria-label="new node goal"
          />
          <button class="connect-btn mono" type="submit" disabled={busyNew || !newGoal.trim()}>
            {busyNew ? 'opening…' : 'create'}
          </button>
        </form>
      {/if}

      {#if showRelocate}
        <form class="mini-form mono" onsubmit={relocate}>
          <input
            class="field mono"
            type="text"
            placeholder="consumer task id (blank = root)"
            bind:value={relocateTarget}
            aria-label="relocate target task"
          />
          <button class="connect-btn mono" type="submit" disabled={busyRelocate}>
            {busyRelocate ? 'moving…' : 'relocate'}
          </button>
        </form>
      {/if}

      {#if error}<p class="error mono">{error}</p>{/if}
      {#if !connected && !error}
        <p class="hint mono">enter a unit slug and connect, or create a node at the root.</p>
      {/if}
    {/snippet}
  </Chat>
</div>

<style>
  .agent-chat-root {
    display: flex;
    flex-direction: column;
    height: 100%;
    /* Intrinsic floors: when the parent has a fixed height (agent page),
       height:100% wins and these are inert. When the parent hugs content
       (gallery card), height:100% resolves to auto and these become the
       effective size — so the component renders fully instead of collapsing. */
    min-height: 440px;
    min-width: 360px;
    max-width: 720px;
    margin: 0 auto;
    width: 100%;
  }
  .hdr {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 14px;
    border-bottom: 1px solid var(--border, #333);
    background: var(--surface, #1a1a1a);
    font-size: 11px;
    flex: 0 0 auto;
  }
  .connect-bar {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 1 1 auto;
    min-width: 0;
  }
  .hdr-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
  }
  .field {
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text, #ddd);
    padding: 4px 8px;
    font-size: 11px;
    min-width: 0;
    flex: 1 1 0;
  }
  .field:focus {
    outline: none;
    border-color: var(--atomizer, #ffb74d);
  }
  .connect-btn {
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text2, #bbb);
    padding: 4px 10px;
    font-size: 11px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    cursor: pointer;
    flex: 0 0 auto;
  }
  .connect-btn:hover:not(:disabled) {
    background: var(--surface3, #2a2a2a);
    color: var(--text, #ddd);
    border-color: var(--atomizer, #ffb74d);
  }
  .connect-btn:disabled { opacity: 0.5; cursor: default; }
  .ctl {
    background: transparent;
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text2, #bbb);
    padding: 4px 8px;
    font-size: 10px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    cursor: pointer;
    flex: 0 0 auto;
  }
  .ctl:hover:not(:disabled) {
    color: var(--text, #ddd);
    border-color: var(--atomizer, #ffb74d);
  }
  .ctl:disabled { opacity: 0.4; cursor: default; }
  .mini-form {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border-bottom: 1px solid var(--border, #333);
    background: var(--surface, #1a1a1a);
    flex: 0 0 auto;
  }
  .state {
    flex: 0 0 auto;
    font-size: 10px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    color: var(--text3, #888);
  }
  .state[data-status='live'] { color: var(--atomizer, #ffb74d); }
  .state[data-status='connecting'] { color: var(--text2, #bbb); }
  .speak-toggle {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--text3, #888);
    cursor: pointer;
    flex: 0 0 auto;
  }
  .speak-toggle input { margin: 0; }
  .error {
    margin: 0;
    padding: 6px 14px;
    background: color-mix(in oklab, var(--warn, #f55) 14%, transparent);
    color: var(--warn, #f55);
    font-size: 12px;
    flex: 0 0 auto;
  }
  .hint {
    margin: 0;
    padding: 6px 14px;
    color: var(--text3, #888);
    font-size: 11px;
    flex: 0 0 auto;
  }
  .mono { font-family: var(--mono, monospace); }
</style>
