<script lang="ts">
  /**
   * The TTS speak/cancel/cooldown morphing button. Shared between the
   * composer and every history bubble so the UI is in lock-step with
   * the single in-flight call:
   *
   *   - idle / done / error → primary "speak"
   *   - mid-call            → ghost "cancel" (clickable, fires oncancel)
   *   - 0.5s after call ends → ghost "cancel" (disabled)  ← anti-double-click
   *
   * The cooldown is local to each instance — they all see the same
   * status flip, so they all morph together. `disabled` from the parent
   * is additive: even in 'speak' mode the parent can keep it disabled
   * (e.g. composer with no text, bubble with nothing to play).
   */
  import { Button } from '@awm/primitives';
  import { onMount } from 'svelte';
  import type { CallStatus } from '$lib/status';

  interface Props {
    status?: CallStatus;
    onspeak?: () => void | Promise<void>;
    oncancel?: () => void;
    /** Extra disable condition for 'speak' mode (text-empty / nothing-to-replay). */
    disabled?: boolean;
    size?: 'sm' | 'md';
    title?: string;
  }
  let {
    status = { kind: 'idle' },
    onspeak,
    oncancel,
    disabled = false,
    size = 'md',
    title,
  }: Props = $props();

  const COOLDOWN_MS = 500;
  let cooling = $state(false);
  let cooldownTimer: ReturnType<typeof setTimeout> | null = null;
  let prevKind: CallStatus['kind'] = 'idle';

  $effect(() => {
    const k = status.kind;
    if (k === 'sending') {
      if (cooldownTimer) { clearTimeout(cooldownTimer); cooldownTimer = null; }
      cooling = false;
    } else if (prevKind === 'sending') {
      cooling = true;
      if (cooldownTimer) clearTimeout(cooldownTimer);
      cooldownTimer = setTimeout(() => { cooling = false; cooldownTimer = null; }, COOLDOWN_MS);
    }
    prevKind = k;
  });

  onMount(() => () => {
    if (cooldownTimer) clearTimeout(cooldownTimer);
  });

  const mode = $derived(
    status.kind === 'sending' ? 'sending' : (cooling ? 'cooldown' : 'speak')
  );
</script>

{#if mode === 'sending'}
  <Button kind="ghost" {size} onclick={() => oncancel?.()} {title}>cancel</Button>
{:else if mode === 'cooldown'}
  <Button kind="ghost" {size} disabled {title}>cancel</Button>
{:else}
  <Button kind="primary" {size} onclick={() => void onspeak?.()} disabled={disabled} {title}>speak</Button>
{/if}
