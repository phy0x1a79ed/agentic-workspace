<script lang="ts">
  import type { Post } from '$lib/api/client';
  import { tick } from 'svelte';

  interface Props {
    posts: Post[];
  }
  let { posts }: Props = $props();

  let scrollEl: HTMLDivElement | undefined = $state();

  $effect(() => {
    // Re-scroll when posts change — defer to next tick so DOM is updated.
    void posts.length;
    tick().then(() => {
      if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
    });
  });

  function shortTs(ts: string | undefined): string {
    if (!ts) return '';
    // Trim ISO timestamps to HH:MM:SS, leave others alone.
    const m = ts.match(/T(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : ts;
  }
</script>

<div class="transcript" bind:this={scrollEl}>
  {#if posts.length === 0}
    <div class="empty-state">
      <div class="empty-icon">◇</div>
      <div class="empty-text">no posts yet</div>
    </div>
  {:else}
    {#each posts as p (p.id ?? p.ts + p.author)}
      <article class="post kind-{p.kind ?? 'text'}">
        <header>
          <span class="author">{p.author}</span>
          <span class="ts mono">{shortTs(p.ts)}</span>
        </header>
        <div class="body">{p.body ?? ''}</div>
      </article>
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
  .ts     { font-size: 10px; color: var(--text3); }
  .body   { color: var(--text); white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.5; }

  .post.kind-system .author { color: var(--text3); }
  .post.kind-slash  .author { color: var(--warn); }

  .mono { font-family: var(--mono); }

  @media (max-width: 720px) {
    .transcript { padding: 12px; gap: 10px; }
  }
</style>
