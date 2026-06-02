<script lang="ts">
  import { Card } from '@awm/primitives';
  import SpeakButton from '$lib/components/SpeakButton.svelte';
  import type { CallStatus } from '$lib/status';

  interface Props {
    text: string;
    status?: CallStatus;
    onspeak?: (text: string) => Promise<void> | void;
    oncancel?: () => void;
  }
  let { text, status = { kind: 'idle' }, onspeak, oncancel }: Props = $props();

  function replay() { void onspeak?.(text); }
</script>

<Card rail="plain">
  <div class="bubble">
    <p class="text">{text}</p>
    <div class="row">
      <SpeakButton
        {status}
        size="sm"
        onspeak={replay}
        oncancel={() => oncancel?.()}
        title="speak this"
      />
      {#if status.kind === 'error'}
        <span class="err">{status.message}</span>
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
