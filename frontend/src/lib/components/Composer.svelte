<script lang="ts">
  import SlashPicker from './SlashPicker.svelte';

  interface Props {
    disabled?: boolean;
    onsubmit?: (text: string) => void | Promise<void>;
    onslashpick?: (cmd: string, autorun: boolean) => void | Promise<void>;
    text?: string;
    slashOpen?: boolean;
    roomId?: string | null;
    scope?: string | null;
  }
  let {
    disabled = false,
    onsubmit,
    onslashpick,
    text = $bindable(''),
    slashOpen = $bindable(false),
    roomId = null,
    scope = null,
  }: Props = $props();

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
    else if (e.key === '/' && text === '') { e.preventDefault(); slashOpen = true; }
  }

  async function pick(cmd: string, autorun: boolean) {
    slashOpen = false;
    if (autorun) {
      // Argless command — dispatch immediately. The parent runs slash via
      // runAgentSlash; for the visible echo we also fire onsubmit equivalence
      // through the dedicated channel (parent handles).
      await onslashpick?.(cmd, true);
    } else {
      // Needs an argument — prefill into input and focus.
      text = cmd + ' ';
      await Promise.resolve();
      inputEl?.focus();
      // Move caret to end.
      try { inputEl?.setSelectionRange(text.length, text.length); } catch { /* noop */ }
    }
  }

  export function focus() { inputEl?.focus(); }
</script>

<div class="composer-wrap">
  {#if slashOpen && roomId}
    <SlashPicker
      {roomId}
      {scope}
      onpick={pick}
      onclose={() => (slashOpen = false)}
    />
  {/if}
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
    <button
      type="button"
      class="btn ghost slash"
      class:active={slashOpen}
      onclick={() => (slashOpen = !slashOpen)}
      {disabled}
      title="slash commands">
      / <span class="caret">▾</span>
    </button>
    <button type="submit" class="btn primary" {disabled}>send</button>
  </form>
</div>

<style>
  .composer-wrap {
    position: relative;
  }
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
  .slash.active {
    background: color-mix(in oklab, var(--atomizer) 18%, transparent);
    color: var(--atomizer);
  }
  .caret { font-size: 9px; color: var(--text3); }

  @media (max-width: 720px) {
    .composer { padding: 10px; }
  }
</style>
