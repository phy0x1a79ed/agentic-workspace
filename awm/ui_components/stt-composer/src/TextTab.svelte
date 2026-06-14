<script lang="ts">
  // Plain, dependable type-and-send box. No STT, no WebSocket, no AI — the
  // reliable escape hatch when voice/LLM is flaky or you just want to type.
  // Exposes the same imperative contract the shell expects from a tab; the
  // voice-only lifecycle methods are inert here (mirrors how
  // ConvoTab.captureCaret was a deliberate no-op).

  interface Props {
    disabled?: boolean;
  }
  let { disabled = false }: Props = $props();

  let value = $state('');
  let textarea: HTMLTextAreaElement | null = $state(null);

  export function consumeText(): string {
    return value.replace(/[ \t]+/g, ' ').trim();
  }
  export function clear() {
    value = '';
  }
  export function focus() {
    textarea?.focus({ preventScroll: true });
  }

  // Voice-only lifecycle — inert in the text tab.
  export function beginLiveChunk() {}
  export function updateLiveChunk(_text: string) {}
  export function finalizeLiveChunk(_text: string) {}
</script>

<div class="text-tab">
  <textarea
    bind:this={textarea}
    bind:value
    class="editor"
    {disabled}
    placeholder="type a message, then SEND…"
    aria-label="text message composer"
    spellcheck="true"
  ></textarea>
</div>

<style>
  .text-tab {
    display: flex;
    flex-direction: column;
    width: 100%;
  }
  .editor {
    min-height: 96px;
    max-height: 240px;
    resize: vertical;
    padding: var(--space-3, 12px);
    background: var(--surface, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 4px);
    color: var(--text, #ddd);
    font-family: var(--sans, system-ui, sans-serif);
    font-size: 14px;
    line-height: 1.6;
    outline: none;
    transition: border-color 0.12s;
  }
  .editor:focus { border-color: var(--atomizer, #ffb74d); }
  .editor:disabled {
    opacity: 0.5;
    background: var(--surface2, #222);
  }
  .editor::placeholder {
    color: var(--text3, #888);
    font-style: italic;
  }
  @media (hover: none), (max-width: 720px) {
    .editor { font-size: 16px; }
  }
</style>
