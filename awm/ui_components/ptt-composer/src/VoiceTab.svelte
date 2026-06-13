<script lang="ts">
  import type { Snippet } from 'svelte';
  import { untrack } from 'svelte';
  import VoiceChip from './VoiceChip.svelte';

  // The UNEDITABLE voice composition space. Two surfaces share it:
  //   - PTT (hold) → a vertically-scrolling list of voice chips, one per
  //     finalized utterance plus a live one that streams partials. Refine by
  //     voice or by removing whole utterances (the chip × button); Clear wipes
  //     everything to retry before sending.
  //   - Convo (`convo` prop) → a single flowing, vertically-scrolling text
  //     panel showing the LLM-cleaned message (`convoText`) as it accumulates
  //     across silence-cuts; completed messages leave for the chat history.
  // There is deliberately no caret and no keyboard text entry here — keyboard
  // editing lives in the Text tab instead.
  //
  // The transport drives this via the same beginLiveChunk / updateLiveChunk /
  // finalizeLiveChunk imperative API the shell forwards to the active tab.
  // The PTT / Convo / Pause controls are transport-owned and rendered into
  // the {@render controls()} slot.

  interface ChipRow { id: string; text: string; live: boolean }

  interface Props {
    // Mock-only seed of finalized chips.
    initialChips?: string[];
    // Transport-owned controls for the right button column (PTT button +
    // the convo play/pause toggle). Rendered between CLEAR and SEND.
    controls?: Snippet;
    // Transport-owned mic-level meter, rendered as a thin strip across the
    // bottom of the voice surface.
    meter?: Snippet;
    // Fired by the column's SEND button; the shell consumes + clears.
    onsend?: () => void;
    // Convo mode: when a continuous session is live the surface switches from
    // the per-utterance chip list (PTT) to a single flowing text panel that
    // shows the LLM-cleaned message as it accumulates, scrolling vertically.
    // `convoText` is the current in-progress message (composer + live tail).
    convo?: boolean;
    convoText?: string;
  }
  let { initialChips, controls, meter, onsend, convo = false, convoText = '' }: Props = $props();

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
  let convoScroller: HTMLDivElement | null = $state(null);
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

  // Keep the convo text panel pinned to the newest text as the message grows.
  $effect(() => {
    convoText; // track
    if (!convo) return;
    queueMicrotask(() => {
      if (convoScroller) convoScroller.scrollTop = convoScroller.scrollHeight;
    });
  });

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
    // Convo: the surface is the single accumulating message, not chips.
    if (convo) return convoText.replace(/[ \t]+/g, ' ').trim();
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
  <div class="main">
    {#if convo}
      <!-- Convo: one flowing, vertically-scrolling text panel showing the
           LLM-cleaned message as it accumulates across silence-cuts. On submit
           the message clears here and lands in the chat history below. -->
      <div
        bind:this={convoScroller}
        class="convo-text"
        class:streaming={convoText.length > 0}
        role="log"
        aria-label="conversation composer"
      >
        {#if convoText}
          <p class="convo-body">{convoText}</p>
        {:else}
          <p class="empty">listening… speak, pause to think, and your message builds here.</p>
        {/if}
      </div>
    {:else}
      <div bind:this={scroller} class="convo-list" role="log" aria-label="dictated utterances">
        {#if chips.length === 0}
          <p class="empty">hold the mic to dictate, or START a conversation…</p>
        {:else}
          {#each chips as chip (chip.id)}
            <VoiceChip
              text={chip.text}
              live={chip.live}
              onremove={() => onRemoveChip(chip.id)}
            />
          {/each}
        {/if}
      </div>
    {/if}

    <div class="button-col">
      <button
        type="button"
        class="col-btn clear"
        onclick={clear}
        disabled={chips.length === 0}
        title="clear and try again"
        aria-label="clear all utterances"
      >CLEAR</button>

      {#if controls}{@render controls()}{/if}

      <button
        type="button"
        class="col-btn send"
        onclick={() => onsend?.()}
        title="send"
        aria-label="send"
      >SEND</button>
    </div>
  </div>

  {#if meter}
    <div class="meter-strip">{@render meter()}</div>
  {/if}
</div>

<style>
  .voice-tab {
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
    width: 100%;
  }

  /* chip list on the left, vertical button column on the right */
  .main {
    display: flex;
    gap: var(--space-2, 8px);
    align-items: stretch;
  }
  /* vertical scroll of full-width utterance cards — mirrors the chat-history
     transcript (darker bg so the surface-colored cards stand out). */
  .convo-list {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 160px;
    max-height: 280px;
    overflow-x: hidden;
    overflow-y: auto;
    padding: var(--space-2, 8px);
    background: var(--bg, #111);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 4px);
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
    align-items: stretch;
  }
  .empty {
    color: var(--text3, #888);
    font-style: italic;
    font-size: 13px;
    margin: 0;
  }

  /* Convo surface: a single flowing message that grows and scrolls vertically,
     replacing the chip list while a continuous session is live. */
  .convo-text {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 160px;
    max-height: 280px;
    overflow-x: hidden;
    overflow-y: auto;
    padding: var(--space-3, 12px);
    background: var(--bg, #111);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 4px);
  }
  .convo-text.streaming {
    border-color: color-mix(in oklab, var(--recording, #f55) 45%, var(--border, #333));
  }
  .convo-body {
    margin: 0;
    color: var(--text, #ddd);
    font-family: var(--sans, system-ui, sans-serif);
    font-size: 14px;
    line-height: 1.6;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  /* Blinking caret trailing the live message. */
  .convo-text.streaming .convo-body::after {
    content: '';
    display: inline-block;
    width: 4px;
    height: 0.95em;
    margin-left: 3px;
    background: var(--recording, #f55);
    vertical-align: text-bottom;
    animation: ptt-pulse 1s ease-in-out infinite;
  }
  @keyframes ptt-pulse {
    0%, 100% { opacity: 0.4; }
    50% { opacity: 1; }
  }

  .button-col {
    flex: 0 0 140px;
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
  }
  /* transport-rendered controls (PTT, convo toggle) stretch to column width */
  .button-col :global(button) { width: 100%; }

  .col-btn {
    min-height: 44px;
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
  .col-btn:hover:not(:disabled) { background: var(--surface3, #2a2a2a); color: var(--text, #ddd); }
  .col-btn:disabled { opacity: 0.35; cursor: not-allowed; }
  .col-btn.clear:hover:not(:disabled) {
    border-color: color-mix(in oklab, var(--danger, #f44) 40%, var(--border, #333));
  }
  .col-btn.send {
    min-height: 52px;
    border-color: color-mix(in oklab, var(--atomizer, #ffb74d) 40%, var(--border, #333));
    color: var(--text, #ddd);
  }
  .col-btn.send:hover {
    background: color-mix(in oklab, var(--atomizer, #ffb74d) 30%, var(--surface2, #222));
    border-color: var(--atomizer, #ffb74d);
  }

  .meter-strip { width: 100%; }

  @media (hover: none), (max-width: 720px) {
    .convo-list, .convo-body { font-size: 16px; }
    .button-col { flex-basis: 120px; }
  }
</style>
