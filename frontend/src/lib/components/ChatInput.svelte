<script lang="ts">
  import SlashPicker from './SlashPicker.svelte';
  import Button from '$lib/components/primitives/Button.svelte';
  import Input from '$lib/components/primitives/Input.svelte';

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

  let inputEl: Input | undefined = $state();

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
      // Move caret to end via the underlying DOM node.
      try { inputEl?.el_()?.setSelectionRange(text.length, text.length); } catch { /* noop */ }
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
    <div class="field">
      <Input
        bind:this={inputEl}
        bind:value={text}
        type="text"
        autocomplete="off"
        placeholder="message or /command"
        onkeydown={onKey}
        {disabled}
      />
    </div>
    <Button
      kind={slashOpen ? 'primary' : 'ghost'}
      onclick={() => (slashOpen = !slashOpen)}
      {disabled}
      title="slash commands">
      / <span class="caret">▾</span>
    </Button>
    <Button kind="primary" type="submit" {disabled}>send</Button>
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
  .field { flex: 1; display: flex; }
  .caret { font-size: 9px; color: var(--text3); }

  @media (max-width: 720px) {
    .composer { padding: 10px; }
  }
</style>
