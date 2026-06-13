<script lang="ts">
  /**
   * Human ↔ live-agent chat, fully self-contained.
   *
   * Composes:
   *   - a project/scope connect bar (this component);
   *   - <PttComposer> from @awm/ptt-composer — text + mic (STT) input;
   *   - <TtsHistory> from @awm/tts-history — the transcript + per-row TTS replay.
   *
   * All business logic lives HERE, not in the page that mounts it. On connect
   * it spawns (busy-tolerant) the scope's agent, seeds the human backlog from
   * the scope channel, and live-subscribes to the agents service transcript
   * (backfill + push, deduped by act id, partials coalesced into one growing
   * bubble per turn). Human input is POSTed to the scope channel; agent acts
   * stream from the agents service. Both are merged into one transcript and
   * rendered through a single <TtsHistory>, with finalized agent messages
   * auto-spoken.
   *
   * Responsibility split (per the plan): the human side talks to the *scope*
   * (messages only); the agent side subscribes to the *agents* service. The
   * frontend never subscribes to the scope channel.
   */
  import { onDestroy, untrack } from 'svelte';
  import {
    fetchScope,
    postToScope,
    spawnAgent,
    subscribeAgent,
    type ScopePost,
    type AgentSubscription,
    type AgentStreamEvent,
  } from '@awm/client';
  import { TtsHistory, playOnce } from '@awm/tts-history';
  import type { Post } from '@awm/tts-history';
  import { PttComposer } from '@awm/ptt-composer';
  import { TranscriptFold, agentAuthor } from './agent-acts';

  interface Props {
    /** Initial project (overrides ?project= / localStorage). */
    project?: string;
    /** Initial scope (overrides ?scope= / localStorage). */
    scope?: string;
    /** Auto-connect on mount when both project+scope resolve. Default true. */
    autoConnect?: boolean;
  }
  let { project: initProject, scope: initScope, autoConnect = true }: Props = $props();

  // --- Connect-bar state (URL ?project=&scope= → localStorage fallback) ----

  const KEY_PROJECT = 'awm.agent.project';
  const KEY_SCOPE = 'awm.agent.scope';

  function readUrlOrStorage(): { project: string; scope: string } {
    if (typeof window === 'undefined') return { project: '', scope: '' };
    const params = new URLSearchParams(window.location.search);
    const project = params.get('project') ?? localStorage.getItem(KEY_PROJECT) ?? '';
    const scope = params.get('scope') ?? localStorage.getItem(KEY_SCOPE) ?? '';
    return { project, scope };
  }

  // One-time seed precedence: explicit prop → URL query → localStorage. Read
  // the props non-reactively (untrack) — they only seed the editable fields;
  // after mount the connect bar owns `project`/`scope`.
  function initialIdentity(): { project: string; scope: string } {
    const s = readUrlOrStorage();
    return { project: initProject ?? s.project, scope: initScope ?? s.scope };
  }
  const seed = untrack(initialIdentity);
  let project = $state(seed.project);
  let scope = $state(seed.scope);

  // Identity of the *connected* session (frozen at connect time so editing the
  // bar mid-session doesn't desync the live subscription).
  let liveProject = $state('');
  let liveScope = $state('');
  let connected = $derived(!!liveProject && !!liveScope);

  // --- Transcript + auto-speak --------------------------------------------

  let posts = $state<Post[]>([]);
  let error = $state<string | null>(null);
  let status = $state<'idle' | 'connecting' | 'live'>('idle');
  let autoSpeak = $state(true);

  // Human posts (own + scope backlog) and agent acts are kept in two ordered
  // buckets and merged by ts for display, so a late human echo and a streaming
  // agent reply interleave correctly in <TtsHistory>.
  let humanPosts = $state<Post[]>([]);
  let fold: TranscriptFold | null = null;
  let sub: AgentSubscription | null = null;

  // Speak de-dupe so a re-render / reconnect can't double-play one message.
  let spokenIds = new Set<string>();

  function rebuild() {
    const agentPosts = fold ? fold.posts : [];
    // Stable merge by ts (ISO compares lexicographically for same-epoch).
    posts = [...humanPosts, ...agentPosts].sort((a, b) =>
      (a.ts ?? '') < (b.ts ?? '') ? -1 : (a.ts ?? '') > (b.ts ?? '') ? 1 : 0,
    );
  }

  function toHumanPost(p: ScopePost): Post {
    return {
      id: p.id ?? `${p.ts}-${p.author}`,
      ts: p.ts,
      author: p.author,
      kind: p.kind ?? 'text',
      body: p.body,
    };
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

  function openAgentStream(p: string, s: string) {
    sub?.close();
    const cursor = fold ? { after_ts: fold.lastTs, after_id: fold.lastId } : {};
    sub = subscribeAgent(p, s, cursor, onAgentEvent);
  }

  function reconnectAgent() {
    if (!connected) return;
    openAgentStream(liveProject, liveScope);
  }

  // --- Connect / disconnect ------------------------------------------------

  async function connect() {
    const p = project.trim();
    const s = scope.trim();
    if (!p || !s) {
      error = 'project and scope are required';
      return;
    }
    disconnect();
    error = null;
    status = 'connecting';
    liveProject = p;
    liveScope = s;
    if (typeof window !== 'undefined') {
      localStorage.setItem(KEY_PROJECT, p);
      localStorage.setItem(KEY_SCOPE, s);
    }

    fold = new TranscriptFold(agentAuthor(p, s));
    spokenIds = new Set();
    humanPosts = [];
    posts = [];

    // Busy-tolerant spawn: a scope that already has a live agent is "attached",
    // not an error — so connecting always self-heals an agent that died.
    try {
      await spawnAgent(p, s);
    } catch (err) {
      error = `spawn failed: ${(err as Error).message}`;
      // Keep going — we can still show the backlog + transcript read-only.
    }

    // Seed the human backlog (messages only) from the scope channel.
    try {
      const backlog = await fetchScope(p, s, { kind: 'message' });
      humanPosts = backlog.map(toHumanPost);
    } catch (err) {
      console.warn('scope backlog fetch failed', err);
    }

    // Live agent stream (backfill + push). The opening backfill frame replays
    // the transcript from the (empty) cursor; no separate fetch needed.
    openAgentStream(p, s);
    status = 'live';
    rebuild();
  }

  function disconnect() {
    sub?.close();
    sub = null;
    fold = null;
    liveProject = '';
    liveScope = '';
    status = 'idle';
  }

  // --- Human input → scope channel ----------------------------------------

  let sending = $state(false);

  async function send(text: string) {
    const t = text.trim();
    if (!t || !connected || sending) return;
    sending = true;
    try {
      const stored = await postToScope(liveProject, liveScope, t);
      // Optimistic local echo — the frontend doesn't subscribe to the scope,
      // so our own message won't come back. Append it to the human bucket.
      humanPosts = [...humanPosts, toHumanPost(stored)];
      rebuild();
    } catch (err) {
      error = `send failed: ${(err as Error).message}`;
    } finally {
      sending = false;
    }
  }

  // PttComposer convo mode emits onText on each silence-segmented utterance;
  // onsend fires on the SEND button. Both post a human turn.
  function onComposerText(text: string) {
    void send(text);
  }

  // Recent transcript fed to the convo cleanup LLM as context (capped on the
  // composer side). Surfacing the last handful of turns keeps the STT cleanup
  // grounded in the live conversation.
  const chatContext = $derived(
    posts.slice(-20).map((p) => `${p.author}: ${p.body}`).join('\n'),
  );

  // --- Lifecycle -----------------------------------------------------------

  $effect(() => {
    if (autoConnect && project.trim() && scope.trim() && status === 'idle') {
      void connect();
    }
  });

  onDestroy(() => disconnect());
</script>

<main class="agent-chat">
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
        placeholder="project"
        bind:value={project}
        aria-label="project"
      />
      <span class="sep">/</span>
      <input
        class="field mono"
        type="text"
        placeholder="scope"
        bind:value={scope}
        aria-label="scope"
      />
      <button class="connect-btn mono" type="submit">
        {connected ? 'reconnect' : 'connect'}
      </button>
      <span class="state mono" data-status={status}>{status}</span>
    </form>
    <label class="speak-toggle mono">
      <input type="checkbox" bind:checked={autoSpeak} /> auto-speak
    </label>
  </header>

  {#if error}<p class="error mono">{error}</p>{/if}
  {#if !connected && !error}
    <p class="hint mono">enter a project + scope and connect to a live agent.</p>
  {/if}

  <div class="history-wrap">
    <TtsHistory {posts} onspeak={speak} />
  </div>

  <div class="composer-wrap">
    <PttComposer
      onsend={onComposerText}
      onText={onComposerText}
      {chatContext}
    />
  </div>
</main>

<style>
  .agent-chat {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
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
  .sep { color: var(--text3, #888); }
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
  .connect-btn:hover {
    background: var(--surface3, #2a2a2a);
    color: var(--text, #ddd);
    border-color: var(--atomizer, #ffb74d);
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
  .history-wrap {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  :global(.history-wrap > .transcript) { flex: 1; }
  .composer-wrap {
    padding: 12px 14px;
    border-top: 1px solid var(--border, #333);
    background: var(--surface, #1a1a1a);
    display: flex;
    justify-content: center;
    flex: 0 0 auto;
  }
  .mono { font-family: var(--mono, monospace); }
</style>
