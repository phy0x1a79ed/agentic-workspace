<script lang="ts">
  import { untrack } from 'svelte';
  import VoiceChip from '../frontend/src/lib/primitives/VoiceChip.svelte';

  // Continuous-conversation tab: a vertically scrolling list of voice chips,
  // one per silence-segmented utterance. The shell drives this via the same
  // beginLiveChunk / updateLiveChunk / finalizeLiveChunk imperative API that
  // PttTab uses, so the panel can dispatch transparently to whichever tab
  // is active.

  interface ChipRow { id: string; text: string; live: boolean }

  interface Props {
    // Mock-only seed of finalized chips.
    initialChips?: string[];
  }
  let { initialChips }: Props = $props();

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

  /** No-op in list mode — there is no caret to capture. The shell calls
   *  this on PTT-down for parity with PttTab. */
  export function captureCaret() {
    // intentionally empty
  }

  /** Append a new live chip (or reuse one already in place). */
  export function beginLiveChunk() {
    liveAbandoned = false;
    if (liveIndex() === -1) {
      chips = [...chips, { id: mkId(), text: '', live: true }];
    }
    scrollToBottom();
  }

  /** Update the live chip's text. If it was destroyed (× tapped, or empty
   *  finalize), respawn one — unless the user explicitly abandoned it. */
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

  /** Finalize the live chip on stt_result. Empty text removes the pill. */
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

  /** Concatenate chip texts with a single space, trim. */
  export function consumeText(): string {
    return chips
      .map((c) => c.text.replace(/[ \t]+/g, ' ').trim())
      .filter((t) => t.length > 0)
      .join(' ')
      .trim();
  }

  /** Drop all chips. Called by the shell's Send after onsend fires. */
  export function clear() {
    chips = [];
    liveAbandoned = false;
  }

  function onRemoveChip(id: string) {
    const idx = chips.findIndex((c) => c.id === id);
    if (idx === -1) return;
    if (chips[idx].live) {
      // Abandon the active utterance — don't respawn from later partials.
      liveAbandoned = true;
    }
    chips = chips.filter((_, i) => i !== idx);
  }
</script>

<div bind:this={scroller} class="convo-list" role="log" aria-label="dictated utterances">
  {#if chips.length === 0}
    <p class="empty">tap the mic to start a continuous dictation session…</p>
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

<style>
  .convo-list {
    min-height: 120px;
    max-height: 240px;
    overflow-y: auto;
    padding: var(--space-3);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    align-items: flex-start;
  }
  .empty {
    color: var(--text3);
    font-style: italic;
    font-size: 13px;
    margin: 0;
  }
  .row {
    display: inline-block;
    max-width: 100%;
  }
  @media (hover: none), (max-width: 720px) {
    .convo-list { font-size: 16px; }
  }
</style>
