<script lang="ts">
  /**
   * The composer: textarea + speak button + volume + round-trip status.
   * Engine selection and config live in the separate `EngineConfigCard`
   * (wrapped with presets in `TtsConfig`); this component only reads the
   * shared `selected`/`params` to drive a speak call.
   */
  import { Card, PanelLabel, Slider } from '@awm/primitives';
  import SpeakButton from '$lib/components/SpeakButton.svelte';
  import type { CallStatus } from '$lib/status';

  interface Props {
    status?: CallStatus;
    selected?: string;
    params?: Record<string, unknown>;
    volume?: number;
    onspeak?: (text: string, engine: string, params: Record<string, unknown>) => Promise<void> | void;
    oncancel?: () => void;
    onvolume?: (v: number) => void;
  }
  let {
    status = { kind: 'idle' },
    selected = $bindable(''),
    params = $bindable({}),
    volume = 1.0,
    onspeak,
    oncancel,
    onvolume,
  }: Props = $props();

  let text = $state('');

  // Lock the textarea (and the keyboard-enter handler) whenever a call
  // is in flight or in its post-call cooldown. Mirrors SpeakButton's
  // own derivation so the two stay aligned.
  let busy = $state(false);
  let prevKind: CallStatus['kind'] = 'idle';
  let cooldownTimer: ReturnType<typeof setTimeout> | null = null;
  $effect(() => {
    const k = status.kind;
    if (k === 'sending') {
      if (cooldownTimer) { clearTimeout(cooldownTimer); cooldownTimer = null; }
      busy = true;
    } else if (prevKind === 'sending') {
      busy = true;
      if (cooldownTimer) clearTimeout(cooldownTimer);
      cooldownTimer = setTimeout(() => { busy = false; cooldownTimer = null; }, 500);
    } else {
      busy = false;
    }
    prevKind = k;
  });

  async function speak() {
    const t = text.trim();
    if (!t || !selected || busy) return;
    try {
      await onspeak?.(t, selected, params);
      text = '';
    } catch {
      // Parent owns status (incl. error display).
    }
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void speak();
    }
  }

  // Live elapsed counter for the in-flight indicator. The parent bumps
  // `tick` (or we just read performance.now() since the dot is animated
  // by CSS); we derive the current elapsed from startedAt + tick to keep
  // Svelte reactive without a per-frame setInterval here.
  const elapsedMs = $derived(
    status.kind === 'sending'
      ? Math.max(0, status.tick - status.startedAt)
      : 0
  );

  function fmtMs(ms: number): string {
    if (ms < 1000) return `${Math.round(ms)} ms`;
    return `${(ms / 1000).toFixed(2)} s`;
  }
</script>

<Card rail="manager">
  <div class="panel">
    <header class="head">
      <PanelLabel tone="atomizer">tts composer</PanelLabel>
      {#if selected}
        <span class="engine-label">{selected}</span>
      {/if}
    </header>

    <textarea
      class="ta mono"
      rows="3"
      placeholder="type something to speak (ctrl/⌘+enter to send)"
      bind:value={text}
      onkeydown={onKey}
      disabled={busy || !selected}
    ></textarea>

    <div class="actions">
      <SpeakButton
        {status}
        size="md"
        onspeak={speak}
        oncancel={() => oncancel?.()}
        disabled={!text.trim() || !selected}
      />
      <span class="hint">ctrl/⌘+enter</span>

      <label class="vol" title="output volume">
        <span class="vol-icon" aria-hidden="true">{volume === 0 ? '🔇' : volume < 0.5 ? '🔈' : volume < 0.9 ? '🔉' : '🔊'}</span>
        <Slider
          value={volume}
          min={0}
          max={1}
          step={0.01}
          precision={2}
          onchange={(v) => onvolume?.(v)}
        />
      </label>

      <div class="status" aria-live="polite">
        {#if status.kind === 'sending'}
          <span class="dot dot-sending" aria-hidden="true"></span>
          <span class="status-text mono">sent · {fmtMs(elapsedMs)}</span>
        {:else if status.kind === 'done'}
          <span class="dot dot-done" aria-hidden="true"></span>
          <span class="status-text mono">audio in {fmtMs(status.latencyMs)}</span>
        {:else if status.kind === 'cancelled'}
          <span class="dot dot-cancelled" aria-hidden="true"></span>
          <span class="status-text mono">cancelled</span>
        {:else if status.kind === 'error'}
          <span class="dot dot-err" aria-hidden="true"></span>
          <span class="status-text status-err mono">{status.message}</span>
        {/if}
      </div>
    </div>
  </div>
</Card>

<style>
  .panel { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-2) 0; }
  .head { display: flex; align-items: center; justify-content: space-between; }
  .engine-label {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--atomizer);
    letter-spacing: 1px;
    text-transform: uppercase;
  }
  .ta {
    width: 100%;
    padding: var(--space-3) var(--space-4);
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    font-size: 13px;
    line-height: 1.4;
    resize: vertical;
    min-height: 70px;
  }
  .ta:focus { outline: none; border-color: var(--atomizer); }
  .mono { font-family: var(--mono); }
  .actions {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    flex-wrap: wrap;
  }
  .hint { color: var(--text3); font-family: var(--mono); font-size: 10px; }

  .vol {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-width: 120px;
    max-width: 180px;
  }
  .vol-icon {
    font-size: 14px;
    line-height: 1;
    user-select: none;
  }

  .status {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    margin-left: auto;
    min-height: 16px;
  }
  .status-text { font-size: 10px; letter-spacing: 0.4px; color: var(--text2); }
  .status-err { color: var(--danger); }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
  }
  .dot-sending {
    background: var(--atomizer);
    box-shadow: 0 0 0 0 color-mix(in oklab, var(--atomizer) 60%, transparent);
    animation: pulse 1100ms var(--ease-mech) infinite;
  }
  .dot-done {
    background: var(--ok, #5fb872);
    opacity: 0.85;
  }
  .dot-cancelled {
    background: var(--text3);
    opacity: 0.85;
  }
  .dot-err {
    background: var(--danger);
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0   color-mix(in oklab, var(--atomizer) 55%, transparent); }
    70%  { box-shadow: 0 0 0 6px color-mix(in oklab, var(--atomizer)  0%, transparent); }
    100% { box-shadow: 0 0 0 0   color-mix(in oklab, var(--atomizer)  0%, transparent); }
  }
</style>
