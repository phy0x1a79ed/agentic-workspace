<script lang="ts">
  import Card from '$lib/primitives/Card.svelte';
  import Button from '$lib/primitives/Button.svelte';

  interface Props {
    text: string;
    onspeak?: (text: string) => Promise<void> | void;
  }
  let { text, onspeak }: Props = $props();

  let state = $state<'idle' | 'playing' | 'error'>('idle');
  let errMsg = $state<string | null>(null);

  async function speak() {
    if (state === 'playing') return;
    state = 'playing';
    errMsg = null;
    try {
      await onspeak?.(text);
      state = 'idle';
    } catch (err) {
      state = 'error';
      errMsg = err instanceof Error ? err.message : String(err);
    }
  }
</script>

<Card rail="plain">
  <div class="bubble">
    <p class="text">{text}</p>
    <div class="row">
      <Button
        kind="primary"
        size="sm"
        onclick={speak}
        disabled={state === 'playing'}
        title={state === 'error' ? errMsg ?? 'error' : 'speak this'}
      >{state === 'playing' ? '…' : 'speak'}</Button>
      {#if state === 'error'}
        <span class="err">{errMsg}</span>
      {/if}
    </div>
  </div>
</Card>

<style>
  .bubble { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-1) 0; }
  .text {
    font-family: var(--sans);
    font-size: 13px;
    line-height: 1.5;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .row { display: flex; align-items: center; gap: var(--space-3); }
  .err { color: var(--danger); font-family: var(--mono); font-size: 10px; }
</style>
