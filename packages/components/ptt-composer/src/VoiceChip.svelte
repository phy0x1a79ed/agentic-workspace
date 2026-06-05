<script lang="ts">
  // Atomic, non-editable voice pill. The visual identity of a transcribed
  // utterance: text + an × delete button, optionally in a "live" streaming
  // state that paints a skeleton until the first partial lands.
  //
  // Used declaratively by ConvoTab.svelte (one element per chip in a list).
  // PttTab.svelte creates the same DOM imperatively via its `makeChunk()`
  // factory because contenteditable + Svelte-state re-renders fight over
  // caret position — but it imports this file so the :global styles ship.

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

<span class="chunk" class:live contenteditable="false">
  <span class="chunk-text">{text}</span>
  <!-- svelte-ignore a11y_consider_explicit_label -->
  <button
    type="button"
    class="chunk-del"
    contenteditable="false"
    tabindex={-1}
    aria-label="delete chunk"
    onclick={handleRemoveClick}
  >×</button>
</span>

<style>
  :global(.chunk) {
    display: inline-flex;
    align-items: center;
    gap: 0;
    padding: 1px 0 1px 8px;
    margin: 0 2px;
    background: color-mix(in oklab, var(--recording, #f55) 16%, var(--surface2, #222));
    border: 1px solid color-mix(in oklab, var(--recording, #f55) 50%, var(--border, #333));
    border-radius: var(--radius-md, 4px);
    color: var(--text, #ddd);
    line-height: 1.5;
    user-select: none;
    vertical-align: baseline;
  }
  :global(.chunk .chunk-text) {
    display: inline-block;
    min-height: 1em;
  }
  :global(.chunk-del) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin: 0 2px 0 8px;
    padding: 0 8px;
    height: 1.4em;
    background: transparent;
    border: 0;
    border-left: 1px solid color-mix(in oklab, var(--recording, #f55) 50%, var(--border, #333));
    color: var(--text2, #bbb);
    font-family: var(--mono, monospace);
    font-size: 13px;
    cursor: pointer;
    user-select: none;
  }
  :global(.chunk-del:hover) { color: var(--danger, #f44); }

  :global(.chunk.live) {
    background: color-mix(in oklab, var(--recording, #f55) 26%, var(--surface2, #222));
    border-color: var(--recording, #f55);
    box-shadow: 0 0 0 1px color-mix(in oklab, var(--recording, #f55) 30%, transparent);
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
