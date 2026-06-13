<script lang="ts">
  // Atomic, non-editable voice card. The visual identity of a transcribed
  // utterance: a full-width section (styled like the chat-history post cards
  // in @awm/tts-history) holding the text, with an × delete strip pinned to
  // the right. The optional "live" streaming state paints a skeleton +
  // recording accent until the first partial lands.
  //
  // Used declaratively by VoiceTab.svelte — one card per chip in the
  // uneditable voice list (both PTT and Convo feed it).

  interface Props {
    text?: string;
    live?: boolean;
    onremove?: () => void;
  }
  let { text = '', live = false, onremove }: Props = $props();

  function handleRemoveClick(e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    onremove?.();
  }
</script>

<div class="chunk" class:live>
  <span class="chunk-text">{text}</span>
  <!-- svelte-ignore a11y_consider_explicit_label -->
  <button
    type="button"
    class="chunk-del"
    tabindex={-1}
    aria-label="delete utterance"
    onclick={handleRemoveClick}
  >×</button>
</div>

<style>
  /* Full-width card, matching the chat-history post cards in @awm/tts-history
     (.post / .body): surface bg, hairline border, 4px radius. */
  :global(.chunk) {
    box-sizing: border-box;
    display: flex;
    align-items: stretch;
    width: 100%;
    background: var(--surface, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 4px);
    color: var(--text, #ddd);
    user-select: none;
    overflow: hidden;
  }
  :global(.chunk .chunk-text) {
    flex: 1 1 auto;
    min-width: 0;
    padding: 5px 10px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
    font-size: 13px;
    line-height: 1.45;
  }
  /* delete strip pinned to the right edge of the card */
  :global(.chunk-del) {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0 10px;
    background: transparent;
    border: 0;
    border-left: 1px solid var(--border, #333);
    color: var(--text3, #888);
    font-family: var(--mono, monospace);
    font-size: 14px;
    line-height: 1;
    cursor: pointer;
    user-select: none;
    transition: color 0.12s, background 0.12s;
  }
  :global(.chunk-del:hover) {
    color: var(--danger, #f44);
    background: color-mix(in oklab, var(--danger, #f44) 12%, transparent);
  }

  :global(.chunk.live) {
    background: color-mix(in oklab, var(--recording, #f55) 12%, var(--surface, #1a1a1a));
    border-color: color-mix(in oklab, var(--recording, #f55) 55%, var(--border, #333));
  }
  :global(.chunk.live .chunk-del) {
    border-left-color: color-mix(in oklab, var(--recording, #f55) 45%, var(--border, #333));
  }
  :global(.chunk.live .chunk-text) {
    color: var(--text, #ddd);
  }
  :global(.chunk.live .chunk-text:empty::before) {
    content: '• • •';
    letter-spacing: 1px;
    color: var(--text2, #bbb);
    font-family: var(--mono, monospace);
    animation: ptt-skel 1s ease-in-out infinite;
  }
  :global(.chunk.live .chunk-text::after) {
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
  @keyframes ptt-skel {
    0%, 100% { opacity: 0.35; }
    50% { opacity: 0.95; }
  }
</style>
