<script lang="ts">
  import Card from '$lib/primitives/Card.svelte';
  import PanelLabel from '$lib/primitives/PanelLabel.svelte';

  interface Props {
    entries: string[];
    placeholder?: string;
  }
  let { entries, placeholder = 'transcripts appear here…' }: Props = $props();

  let scroller: HTMLDivElement | null = $state(null);

  $effect(() => {
    // Re-run on entries change. Pin to bottom — newest line is most relevant.
    entries.length;
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  });
</script>

<Card rail="plain">
  <PanelLabel>Transcripts</PanelLabel>
  <div class="scroll" bind:this={scroller}>
    {#if entries.length === 0}
      <p class="placeholder">{placeholder}</p>
    {:else}
      <ol class="list">
        {#each entries as line, i (i)}
          <li>{line}</li>
        {/each}
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
</style>
