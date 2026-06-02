<script lang="ts">
  import { tick } from 'svelte';

  export interface ChatMessage {
    body: string;
    author?: string;
    ts?: string;
  }

  interface Props {
    messages: ChatMessage[];
  }
  let { messages }: Props = $props();

  let scrollEl: HTMLDivElement | undefined = $state();

  $effect(() => {
    void messages.length;
    tick().then(() => {
      if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
    });
  });

  function shortTs(ts: string | undefined): string {
    if (!ts) return '';
    const m = ts.match(/T(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : ts;
  }
</script>

<div class="transcript" bind:this={scrollEl}>
  {#if messages.length === 0}
    <div class="empty-state">
      <div class="empty-icon">◇</div>
      <div class="empty-text">no posts yet</div>
    </div>
  {:else}
    {#each messages as m, i (i)}
      <article class="post">
        <header>
          <span class="author">{m.author ?? 'you'}</span>
          <span class="ts mono">{shortTs(m.ts)}</span>
        </header>
        <div class="body">{m.body}</div>
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
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    padding: 40px 0;
    color: var(--text3);
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 1px;
  }
  .empty-icon { font-size: 18px; opacity: 0.5; }
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
  .body   { color: var(--text); white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.5; }
  .mono   { font-family: var(--mono); }

  @media (max-width: 720px) {
    .transcript { padding: 12px; gap: 10px; }
  }
</style>
