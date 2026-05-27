<script lang="ts">
  interface Props {
    disabled?: boolean;
    onsubmit?: (text: string) => void | Promise<void>;
    onslashopen?: () => void;
    text?: string;
  }
  let { disabled = false, onsubmit, onslashopen, text = $bindable('') }: Props = $props();

  let inputEl: HTMLInputElement | undefined = $state();

  function send(e?: Event) {
    e?.preventDefault();
    const v = text.trim();
    if (!v || disabled) return;
    onsubmit?.(v);
    text = '';
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) send(e);
  }

  export function focus() { inputEl?.focus(); }
</script>

<form class="composer" onsubmit={send}>
  <input
    bind:this={inputEl}
    class="input field"
    type="text"
    autocomplete="off"
    placeholder="message or /command"
    bind:value={text}
    onkeydown={onKey}
    {disabled} />
  <button type="button" class="btn ghost slash" onclick={() => onslashopen?.()} {disabled} title="slash commands">
    / <span class="caret">▾</span>
  </button>
  <button type="submit" class="btn primary" {disabled}>send</button>
</form>

<style>
  .composer {
    display: flex;
    gap: 6px;
    align-items: stretch;
    padding: 10px 14px;
    border-top: 1px solid var(--border);
    background: var(--surface);
  }
  .field { flex: 1; }
  .slash { white-space: nowrap; }
  .caret { font-size: 9px; color: var(--text3); }

  @media (max-width: 720px) {
    .composer { padding: 10px; }
  }
</style>
