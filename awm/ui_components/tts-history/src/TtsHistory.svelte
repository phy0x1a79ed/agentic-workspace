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

  interface Props {
    posts: Post[];
    /**
     * Speak callback. When provided, agent / user text posts render a
     * speaker affordance whose click invokes this. When omitted, no
     * speak controls.
     */
    onspeak?: (post: Post) => void;
  }
  let { posts, onspeak }: Props = $props();

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
                onclick={() => onspeak?.(g.post)}
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
      linear-gradient(to bottom, color-mix(in oklab, var(--bg, #111) 50%, var(--surface, #1a1a1a)) 0, transparent 60px),
      var(--bg, #111);
  }
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
  .post header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
  }
  .author { font-family: var(--mono, monospace); font-size: 11px; color: var(--atomizer, #ffb74d); letter-spacing: 0.3px; }
  .ts     { font-size: 10px; color: var(--text3, #888); margin-left: auto; }
  .tts-btn {
    background: transparent;
    border: 0;
    padding: 0 4px;
    color: var(--text3, #888);
    cursor: pointer;
    font-size: 11px;
    line-height: 1;
    opacity: 0.6;
  }
  .tts-btn:hover { opacity: 1; color: var(--atomizer, #ffb74d); }
  .body   { color: var(--text, #ddd); white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.5; }

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
