<script lang="ts">
  /**
   * Chat-style transcript with a per-message speak/replay button.
   *
   * Renders posts grouped by membership / tool calls / text. Provide
   * `onspeak` to enable the replay button on speakable rows; the
   * component itself doesn't open TTS calls — the caller decides what
   * "speak" means (`playOnce(...)` from `./lib/api/tts`, a shared
   * `TtsCall`, a synth web worker, …).
   */
  import { tick } from 'svelte';
  import type { Post } from './types';
  import TmuxTerminal from './TmuxTerminal.svelte';

  interface Props {
    posts: Post[];
    /**
     * Speak callback. When provided, agent / user text posts render a
     * speaker affordance whose click invokes this. When omitted, no
     * speak controls.
     */
    onspeak?: (post: Post) => void;
    /**
     * Agent identity (the unit slug). When provided, a Transcript | Terminal
     * tablist appears and the Terminal tab attaches to that agent's live tmux
     * pane (claude-tmux harness). Omit (the default) for a plain transcript —
     * the stt/tts/gallery hosts that have no agent render exactly as before.
     */
    slug?: string;
  }
  let { posts, onspeak, slug }: Props = $props();

  // The Terminal tab exists only when we know which agent to attach to.
  const hasAgent = $derived(!!slug);
  let activeTab = $state<'transcript' | 'terminal'>('transcript');
  // Effective tab: the terminal is only reachable with an agent identity, so a
  // dropped selector transparently falls back to the transcript (no mutation,
  // no self-referential effect).
  const shownTab = $derived(hasAgent ? activeTab : 'transcript');

  let scrollEl: HTMLDivElement | undefined = $state();

  // Tool-group expansion state, keyed by group anchor (first post id or ts+author).
  let expanded = $state<Record<string, boolean>>({});

  type SoloGroup = { kind: 'solo'; key: string; post: Post };
  type MemberGroup = { kind: 'membership'; key: string; items: Post[] };
  type ToolGroup = { kind: 'tools'; key: string; author: string; items: Post[] };
  type Group = SoloGroup | MemberGroup | ToolGroup;

  const MEMBERSHIP = new Set(['join', 'leave']);
  const TOOL = new Set(['tool_use', 'tool_result']);

  function postKey(p: Post): string {
    return String(p.id ?? `${p.ts}-${p.author}`);
  }

  const groups: Group[] = $derived.by(() => {
    const out: Group[] = [];
    for (const p of posts) {
      const k = p.kind ?? 'text';
      const last = out[out.length - 1];
      if (MEMBERSHIP.has(k)) {
        if (last && last.kind === 'membership') {
          last.items.push(p);
        } else {
          out.push({ kind: 'membership', key: postKey(p), items: [p] });
        }
      } else if (TOOL.has(k)) {
        if (last && last.kind === 'tools' && last.author === p.author) {
          last.items.push(p);
        } else {
          out.push({ kind: 'tools', key: postKey(p), author: p.author, items: [p] });
        }
      } else {
        out.push({ kind: 'solo', key: postKey(p), post: p });
      }
    }
    return out;
  });

  $effect(() => {
    void posts.length;
    tick().then(() => {
      if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
    });
  });

  function shortTs(ts: string | undefined): string {
    if (!ts) return '';
    const m = ts.match(/T(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : ts;
  }

  function categorize(a: string): { kind: 'subscriber' | 'agent' | 'other'; label: string } {
    if (a.startsWith('subscriber:')) return { kind: 'subscriber', label: a };
    if (a.startsWith('agent:')) return { kind: 'agent', label: a.slice('agent:'.length) };
    const i = a.indexOf(':');
    return { kind: 'other', label: i >= 0 ? a.slice(i + 1) : a };
  }

  function summarizeSide(items: Post[]): string {
    let subCount = 0;
    const named = new Set<string>();
    for (const p of items) {
      const c = categorize(p.author);
      if (c.kind === 'subscriber') subCount += 1;
      else named.add(c.label);
    }
    const parts: string[] = [];
    for (const n of named) parts.push(n);
    if (subCount > 0) parts.push(`${subCount} subscriber${subCount === 1 ? '' : 's'}`);
    return parts.join(', ');
  }

  function membershipSummary(items: Post[]): string {
    const joined = items.filter(p => p.kind !== 'leave');
    const left = items.filter(p => p.kind === 'leave');
    const parts: string[] = [];
    if (joined.length) parts.push(`${summarizeSide(joined)} joined`);
    if (left.length) parts.push(`${summarizeSide(left)} left`);
    return parts.join(' · ');
  }

  function toolLabel(p: Post): string {
    return p.body ?? '';
  }

  function isSpeakable(p: Post): boolean {
    const k = p.kind ?? 'text';
    if (k !== 'text') return false;
    const a = p.author ?? '';
    return a.startsWith('agent:') || a.startsWith('user:');
  }

  // The human-turn receipt vocabulary (disjoint from the tool running/done set)
  // — the rail renders a receipt glyph for these and a speak button otherwise.
  const USER_STATUSES = new Set(['sending', 'sent', 'received', 'failed']);
  function isHumanStatus(s: string | undefined): boolean {
    return !!s && USER_STATUSES.has(s);
  }
  function receiptGlyph(s: string | undefined): string {
    switch (s) {
      case 'sending': return '⋯';
      case 'sent': return '✓';
      case 'received': return '✓✓';
      case 'failed': return '⚠';
      default: return '';
    }
  }

  // Per-row speak state: the rail's play button flips to a live equalizer while
  // its message is being spoken. `onspeak` may return a promise (the agent-chat
  // host forwards `playOnce(...)`); we toggle the state off when it settles so
  // the button reflects real playback, not a fixed timer.
  let speakingKey = $state<string | undefined>(undefined);
  async function doSpeak(p: Post): Promise<void> {
    const k = postKey(p);
    if (speakingKey === k) return; // already playing this row
    speakingKey = k;
    try {
      await onspeak?.(p);
    } finally {
      if (speakingKey === k) speakingKey = undefined;
    }
  }
</script>

{#snippet transcriptPane()}
<div class="transcript" bind:this={scrollEl} data-awm-component="TtsHistory">
  {#if groups.length === 0}
    <div class="empty-state">
      <div class="empty-icon">◇</div>
      <div class="empty-text">no posts yet</div>
    </div>
  {:else}
    {#each groups as g (g.key)}
      {#if g.kind === 'membership'}
        <div class="membership mono">
          <span class="dot">·</span>
          <span class="text">{membershipSummary(g.items)}</span>
          <span class="ts">{shortTs(g.items[g.items.length - 1].ts)}</span>
        </div>
      {:else if g.kind === 'tools'}
        {@const running = g.items.some((t) => t.kind === 'tool_use' && t.status === 'running')}
        <article class="post kind-tools">
          <header>
            <button class="tool-toggle" type="button" onclick={() => (expanded[g.key] = !expanded[g.key])}>
              <span class="caret">{expanded[g.key] ? '▾' : '▸'}</span>
              <span class="author">{g.author}</span>
              <span class="count mono">{g.items.length} tool {g.items.length === 1 ? 'call' : 'calls'}</span>
              {#if running}
                <span class="running-dot" aria-label="running" title="running"></span>
              {/if}
            </button>
            <span class="ts mono">{shortTs(g.items[g.items.length - 1].ts)}</span>
          </header>
          {#if expanded[g.key]}
            <ul class="tool-list">
              {#each g.items as t (postKey(t))}
                <li class="tool-row kind-{t.kind ?? 'text'}" class:running={t.kind === 'tool_use' && t.status === 'running'}>
                  <span class="tool-kind mono">{t.kind === 'tool_result' ? '←' : t.status === 'running' ? '◌' : '→'}</span>
                  <span class="tool-body mono">{toolLabel(t)}</span>
                </li>
              {/each}
            </ul>
          {/if}
        </article>
      {:else}
        {@const sp = g.post}
        {@const speaking = speakingKey === postKey(sp)}
        {@const working = sp.status === 'working'}
        <article
          class="post chip kind-{sp.kind ?? 'text'}"
          data-status={sp.status ?? ''}
          class:dim={sp.status === 'sending' || sp.status === 'failed'}
          class:working
        >
          <div class="post-main">
            <span class="author">{sp.author}</span>
            {#if working}
              <!-- Turn-start skeleton: the agent is acting (a tool call or an
                   internal turn) but has produced no text yet. Cleared on the
                   turn's first real content / result. -->
              <div class="body working-ind" aria-label="agent working">
                <span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>
              </div>
            {:else}
              <div class="body">{sp.body ?? ''}</div>
            {/if}
          </div>
          <aside class="rail">
            <span class="ts mono">{shortTs(sp.ts)}</span>
            {#if working}
              <!-- no receipt / no speak button for the transient skeleton -->
            {:else if isHumanStatus(sp.status)}
              <span class="receipt" data-status={sp.status} title={sp.status} aria-label={sp.status}>
                {receiptGlyph(sp.status)}
              </span>
            {:else if onspeak && isSpeakable(sp)}
              <button
                class="speak"
                type="button"
                data-speaking={speaking}
                title={speaking ? 'speaking' : 'replay message'}
                aria-label={speaking ? 'speaking' : 'replay message'}
                onclick={() => doSpeak(sp)}
              >
                {#if speaking}
                  <span class="eq" aria-hidden="true"><i></i><i></i><i></i></span>
                {:else}
                  <span class="glyph" aria-hidden="true">▶</span>
                {/if}
              </button>
            {/if}
          </aside>
        </article>
      {/if}
    {/each}
  {/if}
</div>
{/snippet}

{#if hasAgent}
  <div class="tts-history">
    <div class="tablist" role="tablist" aria-label="agent view">
      <button
        type="button"
        role="tab"
        aria-selected={shownTab === 'transcript'}
        class:active={shownTab === 'transcript'}
        onclick={() => (activeTab = 'transcript')}
      >Transcript</button>
      <button
        type="button"
        role="tab"
        aria-selected={shownTab === 'terminal'}
        class:active={shownTab === 'terminal'}
        onclick={() => (activeTab = 'terminal')}
      >Terminal</button>
    </div>
    {#if shownTab === 'transcript'}
      {@render transcriptPane()}
    {:else}
      <!-- Mounted only while visible so xterm fits a laid-out container; the
           keyed block re-mounts (re-attaches) when the agent identity changes. -->
      {#key slug}
        <TmuxTerminal slug={slug!} />
      {/key}
    {/if}
  </div>
{:else}
  {@render transcriptPane()}
{/if}

<style>
  .tts-history {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .tablist {
    flex: 0 0 auto;
    display: flex;
    gap: 2px;
    padding: 4px 6px 0;
    border-bottom: 1px solid var(--border, #333);
    background: var(--surface, #1a1a1a);
  }
  .tablist button {
    appearance: none;
    background: transparent;
    border: 1px solid transparent;
    border-bottom: none;
    color: var(--text3, #888);
    font: inherit;
    font-size: 0.78rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 5px 12px;
    border-radius: 4px 4px 0 0;
    cursor: pointer;
  }
  .tablist button:hover { color: var(--text2, #bbb); }
  .tablist button.active {
    color: var(--text, #ddd);
    background: var(--bg, #111);
    border-color: var(--border, #333);
  }
  /* When tabbed, the transcript is no longer a direct child of the host's
     `.history-wrap`, so re-assert the grow it would otherwise inherit. */
  .tts-history > .transcript { flex: 1 1 auto; min-height: 0; }
  .transcript {
    flex: 1;
    overflow-y: auto;
    padding: 14px 18px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    /* Intrinsic floor: standalone (gallery card) the transcript has no
       height-constrained flex parent, so without this it collapses to its
       content. Inside a height-constrained flex parent (agent/stt pages)
       flex:1 grows it past this floor, so the floor is inert there. */
    min-height: 280px;
    position: relative;
    background:
      linear-gradient(to bottom, color-mix(in oklab, var(--bg, #111) 50%, var(--surface, #1a1a1a)) 0, transparent 60px),
      var(--bg, #111);
    /* Themed slim scrollbar — Firefox. */
    scrollbar-width: thin;
    scrollbar-color: var(--border, #333) transparent;
  }
  /* Themed slim scrollbar — WebKit/Blink. */
  .transcript::-webkit-scrollbar { width: 8px; }
  .transcript::-webkit-scrollbar-track { background: transparent; }
  .transcript::-webkit-scrollbar-thumb {
    background: var(--border, #333);
    border-radius: 4px;
  }
  .transcript::-webkit-scrollbar-thumb:hover { background: var(--text3, #888); }
  .transcript::after {
    content: '';
    position: absolute; inset: 0; pointer-events: none;
    background-image: repeating-linear-gradient(to bottom, transparent 0 2px, rgba(255,255,255,0.012) 2px 3px);
  }
  .post {
    background: var(--surface, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 8px 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  /* A solo chip is a two-track row: the message body on the left, a fixed-width
     status rail on the right (timestamp pinned top; a speak button on agent
     turns / a delivery receipt on human turns below it). The rail is a
     flex-fixed item so a long body wraps in the left track instead of
     squeezing the 44px touch target. */
  .post.chip {
    flex-direction: row;
    align-items: stretch;
    gap: 10px;
    padding: 0;
  }
  .post-main {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 8px 0 8px 12px;
  }
  .rail {
    flex: 0 0 52px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    padding: 8px 0;
    border-left: 1px solid var(--border, #333);
  }
  .post.kind-tools header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
  }
  .author { font-family: var(--mono, monospace); font-size: 11px; color: var(--atomizer, #ffb74d); letter-spacing: 0.3px; }
  .ts     { font-size: 10px; color: var(--text3, #888); }
  .rail .ts { width: 100%; text-align: center; }
  .body   { color: var(--text, #ddd); white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.5; }

  /* Body dims while the human turn is unconfirmed (sending) or failed. */
  .post.dim .body { opacity: 0.55; }

  /* Turn-start "agent working" skeleton — an animated three-dot indicator that
     shows the agent is acting before its first token (a tool call or an internal
     turn). Cleared the moment the turn yields real content / ends. */
  .post.working { opacity: 0.85; }
  .working-ind { display: flex; align-items: center; min-height: 1.2em; }
  .working-ind .dots { display: inline-flex; gap: 4px; }
  .working-ind .dots i {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--atomizer, #ffb74d);
    animation: awm-working 1.1s ease-in-out infinite;
  }
  .working-ind .dots i:nth-child(2) { animation-delay: 0.18s; }
  .working-ind .dots i:nth-child(3) { animation-delay: 0.36s; }
  @keyframes awm-working {
    0%, 80%, 100% { opacity: 0.25; transform: translateY(0); }
    40% { opacity: 1; transform: translateY(-2px); }
  }

  /* Speak button — a 44×44 square that flips to a live equalizer mid-playback. */
  .speak {
    width: 44px;
    height: 44px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    color: var(--text3, #888);
    cursor: pointer;
    padding: 0;
    transition: color 0.12s, border-color 0.12s;
  }
  .speak:hover { color: var(--atomizer, #ffb74d); border-color: var(--atomizer, #ffb74d); }
  .speak:focus-visible {
    outline: 2px solid var(--atomizer, #ffb74d);
    outline-offset: 2px;
    color: var(--atomizer, #ffb74d);
  }
  .speak[data-speaking='true'] { border-color: var(--atomizer, #ffb74d); }
  .speak .glyph { font-size: 18px; line-height: 1; }
  /* Equalizer: three bars rising and falling while the message is spoken. */
  .speak .eq { display: inline-flex; align-items: flex-end; gap: 3px; height: 20px; }
  .speak .eq i {
    width: 3px;
    height: 100%;
    background: var(--atomizer, #ffb74d);
    transform-origin: bottom;
    animation: eq-bounce 0.9s ease-in-out infinite;
  }
  .speak .eq i:nth-child(2) { animation-delay: 0.18s; }
  .speak .eq i:nth-child(3) { animation-delay: 0.36s; }
  @keyframes eq-bounce {
    0%, 100% { transform: scaleY(0.35); }
    50%      { transform: scaleY(1); }
  }

  /* Delivery receipt — the human turn's lifecycle, mono glyphs (no emoji). */
  .receipt {
    font-family: var(--mono, monospace);
    font-size: 13px;
    line-height: 1;
    color: var(--text3, #888);
    min-height: 16px;
  }
  .receipt[data-status='received'] { color: var(--atomizer, #ffb74d); }
  .receipt[data-status='failed']   { color: var(--warn, #f55); }
  .receipt[data-status='sending']  { color: var(--text3, #888); opacity: 0.7; }

  /* Tool-call running indicator — a CSS-pulsed amber dot, no glyph. */
  .running-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--atomizer, #ffb74d);
    display: inline-block;
    margin-left: 2px;
    animation: dot-pulse 1.1s ease-in-out infinite;
  }
  @keyframes dot-pulse {
    0%, 100% { opacity: 0.3; }
    50%      { opacity: 1; }
  }
  .tool-row.running .tool-kind { color: var(--atomizer, #ffb74d); animation: dot-pulse 1.1s ease-in-out infinite; }

  @media (prefers-reduced-motion: reduce) {
    .speak .eq i,
    .running-dot,
    .tool-row.running .tool-kind { animation: none; }
    .speak .eq i { transform: scaleY(0.6); }
    .running-dot { opacity: 0.8; }
  }

  .post.kind-system .author { color: var(--text3, #888); }
  .post.kind-slash  .author { color: var(--warn, #f55); }

  .mono { font-family: var(--mono, monospace); }

  .membership {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 0 4px;
    color: var(--text3, #888);
    font-size: 10px;
    letter-spacing: 0.3px;
    margin: -4px 0;
  }
  .membership .dot { opacity: 0.5; }
  .membership .text { flex: 1; }
  .membership .ts { color: var(--text3, #888); }

  .post.kind-tools { gap: 0; }
  .tool-toggle {
    background: none;
    border: 0;
    padding: 0;
    cursor: pointer;
    display: inline-flex;
    align-items: baseline;
    gap: 8px;
    color: var(--text2, #bbb);
    text-align: left;
  }
  .tool-toggle .caret { color: var(--text3, #888); width: 10px; display: inline-block; font-size: 10px; }
  .tool-toggle .count { color: var(--text3, #888); font-size: 10px; letter-spacing: 0.3px; }
  .tool-list {
    list-style: none;
    margin: 8px 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    border-top: 1px solid var(--border, #333);
    padding-top: 8px;
  }
  .tool-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 11px;
    color: var(--text, #ddd);
  }
  .tool-row .tool-kind { color: var(--text3, #888); width: 10px; flex: 0 0 auto; }
  .tool-row .tool-body { white-space: pre-wrap; word-break: break-word; flex: 1; }
  .tool-row.kind-tool_result .tool-body { color: var(--text2, #bbb); }

  .empty-state {
    margin: auto;
    text-align: center;
    color: var(--text3, #888);
    font-family: var(--mono, monospace);
    font-size: 11px;
  }
  .empty-icon { font-size: 24px; opacity: 0.4; margin-bottom: 4px; }

  @media (max-width: 720px) {
    .transcript { padding: 12px; gap: 10px; }
  }
</style>
