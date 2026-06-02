<script lang="ts">
  import { Button, Card } from '@awm/primitives';

  interface Props {
    text: string;
    onspeak?: (text: string) => Promise<void> | void;
  }
  let { text, onspeak }: Props = $props();

  let phase = $state<'idle' | 'playing' | 'error'>('idle');
  let errMsg = $state<string | null>(null);

  async function speak() {
    if (phase === 'playing') return;
    phase = 'playing';
    errMsg = null;
    try {
      await onspeak?.(text);
      phase = 'idle';
    } catch (err) {
      phase = 'error';
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
        disabled={phase === 'playing'}
        title={phase === 'error' ? errMsg ?? 'error' : 'speak this'}
      >{phase === 'playing' ? '…' : 'speak'}</Button>
      {#if phase === 'error'}
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
