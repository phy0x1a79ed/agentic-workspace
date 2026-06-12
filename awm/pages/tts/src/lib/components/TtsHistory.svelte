<script lang="ts">
  import TtsBubble from './TtsBubble.svelte';
  import type { CallStatus } from '$lib/status';

  interface Bubble { id: string; text: string; }
  interface Props {
    bubbles: Bubble[];
    status?: CallStatus;
    onspeak?: (text: string) => Promise<void> | void;
    oncancel?: () => void;
  }
  let { bubbles, status = { kind: 'idle' }, onspeak, oncancel }: Props = $props();

  let scrollEl: HTMLDivElement | undefined = $state();
  $effect(() => {
    void bubbles.length;
    requestAnimationFrame(() => {
      scrollEl?.scrollTo({ top: scrollEl.scrollHeight, behavior: 'smooth' });
    });
  });
</script>

<div class="hist" bind:this={scrollEl}>
  {#if bubbles.length === 0}
    <div class="empty">no utterances yet</div>
  {/if}
  {#each bubbles as b (b.id)}
    <TtsBubble text={b.text} {status} {onspeak} {oncancel} />
  {/each}
</div>

<style>
  .hist {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    overflow-y: auto;
    padding: var(--space-3);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    min-height: 0;
    flex: 1 1 auto;
  }
  .empty {
    color: var(--text3);
    font-family: var(--mono);
    font-size: 11px;
    font-style: italic;
    padding: var(--space-4);
    text-align: center;
  }
</style>
