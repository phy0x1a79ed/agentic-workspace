<script lang="ts">
  import Card from '$lib/primitives/Card.svelte';
  import PanelLabel from '$lib/primitives/PanelLabel.svelte';

  interface Props {
    entries: string[];
    currentPartial?: string | null;
    // Whether the partial is currently growing (drives the trailing "…").
    // True while the user is still holding the button; false once `end`
    // fires and the panel is waiting for the final result.
    live?: boolean;
    placeholder?: string;
  }
  let {
    entries,
    currentPartial = null,
    live = false,
    placeholder = 'transcripts appear here…',
  }: Props = $props();

  let scroller: HTMLDivElement | null = $state(null);

  $effect(() => {
    // Re-run on entries OR partial change. Pin to bottom — newest line and
    // the rolling partial are both most-relevant.
    entries.length;
    currentPartial;
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  });

  let hasPartial = $derived(typeof currentPartial === 'string' && currentPartial.length > 0);
</script>

<Card rail="plain">
  <PanelLabel>Transcripts</PanelLabel>
  <div class="scroll" bind:this={scroller}>
    {#if entries.length === 0 && !hasPartial}
      <p class="placeholder">{placeholder}</p>
    {:else}
      <ol class="list">
        {#each entries as line, i (i)}
          <li>{line}</li>
        {/each}
        {#if hasPartial}
          <li class="partial" class:live>{currentPartial}</li>
        {/if}
      </ol>
    {/if}
  </div>
</Card>

<style>
  .scroll {
    margin-top: var(--space-2);
    max-height: 240px;
    min-height: 80px;
    overflow-y: auto;
    background: var(--surface1);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: var(--space-2);
  }
  .placeholder {
    margin: 0;
    color: var(--text3);
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.5px;
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .list li {
    color: var(--text);
    font-family: var(--sans);
    font-size: 13px;
    line-height: 1.4;
    padding: 4px 6px;
    border-left: 2px solid var(--border);
    word-wrap: break-word;
  }
  .list li:last-child {
    border-left-color: var(--atomizer);
  }
  .list li.partial {
    color: var(--text3);
    font-style: italic;
    border-left-color: var(--recording);
  }
  .list li.partial.live::after {
    content: '…';
    margin-left: 2px;
    color: var(--recording);
    animation: ptt-pulse 1s ease-in-out infinite;
  }
  @keyframes ptt-pulse {
    0%, 100% { opacity: 0.4; }
    50% { opacity: 1; }
  }
</style>
