<script lang="ts">
  import { tick } from 'svelte';
  import type { Post } from './types';

  interface Props {
    posts: Post[];
    /**
     * Speak callback. When provided, agent / user text posts render a speaker
     * affordance whose click invokes this. When omitted, no speak controls.
     * The caller decides what playback means (legacy /voice/tts/speak, the
     * @awm/tts stripe, a synth web worker, …).
     */
    onspeak?: (post: Post) => void;
  }
  let { posts, onspeak }: Props = $props();

  let scrollEl: HTMLDivElement | undefined = $state();

  // Tool-group expansion state, keyed by group anchor (first post id or ts+author).
  // Plain object (not $state) — Svelte 5 re-renders via posts dep anyway.
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
    // Re-scroll when posts change — defer to next tick so DOM is updated.
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

  // Author tokens look like "kind:identifier" — e.g. "agent:_vagrant/user-x",
  // "subscriber:ws:139029516135488:user:operator" (each browser tab attach).
  // Subscriber rows are ephemeral noise (every page reload churns them) so
  // we collapse them to a count; real agent joins show their bare scope.
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
    // tool_use body is "[tool_use: name]"; tool_result body is the truncated payload.
    return p.body ?? '';
  }

  // Speakable posts: agent or user text (not subscriber, not system, not
  // tool_use/tool_result). Caller's onspeak handles the actual playback.
  function isSpeakable(p: Post): boolean {
    const k = p.kind ?? 'text';
    if (k !== 'text') return false;
    const a = p.author ?? '';
    return a.startsWith('agent:') || a.startsWith('user:');
  }
</script>

<div class="transcript" bind:this={scrollEl}>
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
        <article class="post kind-tools">
          <header>
            <button class="tool-toggle" type="button" onclick={() => (expanded[g.key] = !expanded[g.key])}>
              <span class="caret">{expanded[g.key] ? '▾' : '▸'}</span>
              <span class="author">{g.author}</span>
              <span class="count mono">⚙ {g.items.length} tool {g.items.length === 1 ? 'call' : 'calls'}</span>
            </button>
            <span class="ts mono">{shortTs(g.items[g.items.length - 1].ts)}</span>
          </header>
          {#if expanded[g.key]}
            <ul class="tool-list">
              {#each g.items as t (postKey(t))}
                <li class="tool-row kind-{t.kind ?? 'text'}">
                  <span class="tool-kind mono">{t.kind === 'tool_result' ? '←' : '→'}</span>
                  <span class="tool-body mono">{toolLabel(t)}</span>
                </li>
              {/each}
            </ul>
          {/if}
        </article>
      {:else}
        <article class="post kind-{g.post.kind ?? 'text'}">
          <header>
            <span class="author">{g.post.author}</span>
            {#if onspeak && isSpeakable(g.post)}
              <button
                class="tts-btn"
                type="button"
                title="replay"
                aria-label="replay"
                onclick={() => onspeak(g.post)}
              >🔊</button>
            {/if}
            <span class="ts mono">{shortTs(g.post.ts)}</span>
          </header>
          <div class="body">{g.post.body ?? ''}</div>
        </article>
      {/if}
    {/each}
  {/if}
</div>

<style>
  .transcript {
    flex: 1;
    overflow-y: auto;
    padding: 14px 18px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-height: 0;
    position: relative;
    background:
      linear-gradient(to bottom, color-mix(in oklab, var(--bg) 50%, var(--surface)) 0, transparent 60px),
      var(--bg);
  }
  .transcript::after {
    content: '';
    position: absolute; inset: 0; pointer-events: none;
    background-image: repeating-linear-gradient(to bottom, transparent 0 2px, rgba(255,255,255,0.012) 2px 3px);
  }
  .post {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 8px 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .post header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
  }
  .author { font-family: var(--mono); font-size: 11px; color: var(--atomizer); letter-spacing: 0.3px; }
  .ts     { font-size: 10px; color: var(--text3); margin-left: auto; }
  .tts-btn {
    background: transparent;
    border: 0;
    padding: 0 4px;
    color: var(--text3);
    cursor: pointer;
    font-size: 11px;
    line-height: 1;
    opacity: 0.6;
  }
  .tts-btn:hover { opacity: 1; color: var(--atomizer); }
  .body   { color: var(--text); white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.5; }

  .post.kind-system .author { color: var(--text3); }
  .post.kind-slash  .author { color: var(--warn); }

  .mono { font-family: var(--mono); }

  /* membership one-liner — no card chrome */
  .membership {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 0 4px;
    color: var(--text3);
    font-size: 10px;
    letter-spacing: 0.3px;
    margin: -4px 0;
  }
  .membership .dot { opacity: 0.5; }
  .membership .text { flex: 1; }
  .membership .ts { color: var(--text3); }

  /* tool group */
  .post.kind-tools { gap: 0; }
  .tool-toggle {
    background: none;
    border: 0;
    padding: 0;
    cursor: pointer;
    display: inline-flex;
    align-items: baseline;
    gap: 8px;
    color: var(--text2);
    text-align: left;
  }
  .tool-toggle .caret { color: var(--text3); width: 10px; display: inline-block; font-size: 10px; }
  .tool-toggle .count { color: var(--text3); font-size: 10px; letter-spacing: 0.3px; }
  .tool-list {
    list-style: none;
    margin: 8px 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    border-top: 1px solid var(--border);
    padding-top: 8px;
  }
  .tool-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 11px;
    color: var(--text);
  }
  .tool-row .tool-kind { color: var(--text3); width: 10px; flex: 0 0 auto; }
  .tool-row .tool-body { white-space: pre-wrap; word-break: break-word; flex: 1; }
  .tool-row.kind-tool_result .tool-body { color: var(--text2); }

  @media (max-width: 720px) {
    .transcript { padding: 12px; gap: 10px; }
  }
</style>
