<script lang="ts">
  import type { Snippet } from 'svelte';
  import { untrack } from 'svelte';
  import VoiceChip from './VoiceChip.svelte';

  // The single, UNEDITABLE voice composition space. Both push-to-talk (hold)
  // and continuous conversation feed this one vertically-scrolling list of
  // voice chips — one per finalized utterance, plus a live one that streams
  // partials. There is deliberately no caret and no keyboard text entry:
  // you refine by voice or by removing whole utterances (the chip × button),
  // and Clear wipes everything to retry before sending. Keyboard editing
  // lives in the Text tab instead.
  //
  // The transport drives this via the same beginLiveChunk / updateLiveChunk /
  // finalizeLiveChunk imperative API the shell forwards to the active tab.
  // The PTT / Convo / Pause controls are transport-owned and rendered into
  // the {@render controls()} slot.

  interface ChipRow { id: string; text: string; live: boolean }

  interface Props {
    // Mock-only seed of finalized chips.
    initialChips?: string[];
    // Transport-owned control row (PTT button, Convo start/stop, pause).
    controls?: Snippet;
  }
  let { initialChips, controls }: Props = $props();

  let chips = $state<ChipRow[]>(untrack(() => (
    initialChips
      ? initialChips.filter(Boolean).map((t, i) => ({
          id: `seed${i}`,
          text: t,
          live: false,
        }))
      : []
  )));

  let scroller: HTMLDivElement | null = $state(null);
  let nextId = 0;
  let liveAbandoned = false;

  function mkId(): string {
    nextId += 1;
    return `c${nextId}`;
  }

  function scrollToBottom() {
    queueMicrotask(() => {
      if (!scroller) return;
      scroller.scrollTop = scroller.scrollHeight;
    });
  }

  function liveIndex(): number {
    for (let i = chips.length - 1; i >= 0; i -= 1) {
      if (chips[i].live) return i;
    }
    return -1;
  }

  // No caret in list mode — kept so the shell's dispatch contract is uniform.
  export function captureCaret() {}

  export function beginLiveChunk() {
    liveAbandoned = false;
    if (liveIndex() === -1) {
      chips = [...chips, { id: mkId(), text: '', live: true }];
    }
    scrollToBottom();
  }

  export function updateLiveChunk(text: string) {
    if (liveAbandoned) return;
    const idx = liveIndex();
    if (idx === -1) {
      chips = [...chips, { id: mkId(), text, live: true }];
      scrollToBottom();
      return;
    }
    chips[idx].text = text;
  }

  export function finalizeLiveChunk(text: string) {
    const wasAbandoned = liveAbandoned;
    liveAbandoned = false;
    const idx = liveIndex();
    if (wasAbandoned) {
      if (idx >= 0) chips = chips.filter((_, i) => i !== idx);
      return;
    }
    if (idx === -1) {
      if (!text.trim()) return;
      chips = [...chips, { id: mkId(), text, live: false }];
      scrollToBottom();
      return;
    }
    if (!text.trim()) {
      chips = chips.filter((_, i) => i !== idx);
      return;
    }
    chips[idx].text = text;
    chips[idx].live = false;
  }

  export function consumeText(): string {
    return chips
      .map((c) => c.text.replace(/[ \t]+/g, ' ').trim())
      .filter((t) => t.length > 0)
      .join(' ')
      .trim();
  }

  export function clear() {
    chips = [];
    liveAbandoned = false;
  }

  function onRemoveChip(id: string) {
    const idx = chips.findIndex((c) => c.id === id);
    if (idx === -1) return;
    if (chips[idx].live) {
      liveAbandoned = true;
    }
    chips = chips.filter((_, i) => i !== idx);
  }
</script>

<div class="voice-tab">
  <div bind:this={scroller} class="convo-list" role="log" aria-label="dictated utterances">
    {#if chips.length === 0}
      <p class="empty">hold the mic to dictate, or START a conversation…</p>
    {:else}
      {#each chips as chip (chip.id)}
        <span class="row">
          <VoiceChip
            text={chip.text}
            live={chip.live}
            onremove={() => onRemoveChip(chip.id)}
          />
        </span>
      {/each}
    {/if}
  </div>

  {#if controls}
    <div class="controls">{@render controls()}</div>
  {/if}

  <div class="actions">
    <button
      type="button"
      class="clear-btn"
      onclick={clear}
      disabled={chips.length === 0}
      title="clear and try again"
      aria-label="clear all utterances"
    >CLEAR</button>
  </div>
</div>

<style>
  .voice-tab {
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
    width: 100%;
  }
  .convo-list {
    min-height: 120px;
    max-height: 240px;
    overflow-y: auto;
    padding: var(--space-3, 12px);
    background: var(--surface, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 4px);
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
    align-items: flex-start;
  }
  .empty {
    color: var(--text3, #888);
    font-style: italic;
    font-size: 13px;
    margin: 0;
  }
  .row {
    display: inline-block;
    max-width: 100%;
  }
  .controls {
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
  }
  .actions {
    display: flex;
    justify-content: flex-end;
  }
  .clear-btn {
    min-height: 32px;
    padding: 0 12px;
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    color: var(--text2, #bbb);
    font-family: var(--mono, monospace);
    font-size: 11px;
    letter-spacing: 1px;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .clear-btn:hover:not(:disabled) {
    background: var(--surface3, #2a2a2a);
    color: var(--text, #ddd);
    border-color: color-mix(in oklab, var(--danger, #f44) 40%, var(--border, #333));
  }
  .clear-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  @media (hover: none), (max-width: 720px) {
    .convo-list { font-size: 16px; }
  }
</style>
