<script lang="ts">
  /**
   * Coloured pulse dot + ping readout. Color encodes link state (ui.wsKind +
   * ping latency); voice activity (recording/transcribing) overrides with the
   * recording colour. The dot pulses at the same cadence the ping loop
   * refreshes at, so visually you can tell when the link is sluggish.
   */
  import { ui } from '$lib/state/ui.svelte';
  import { voice } from '$lib/state/voice.svelte';

  type State = 'off' | 'err' | 'ok' | 'slow' | 'active';

  // Same formula as ui._pingTick: cadence = max(2*ping, 1000ms). Drives the
  // pulse animation so the dot beats once per ping cycle.
  const SLOW_MS = 300;

  const state = $derived<State>(
    (voice.stage === 'recording' || voice.stage === 'transcribing') ? 'active' :
    ui.wsKind === 'off' ? 'off' :
    ui.wsKind === 'err' ? 'err' :
    (ui.pingMs ?? 0) > SLOW_MS ? 'slow' :
    'ok'
  );

  // Visual pulse is intentionally much slower than the refresh cadence — the
  // ping number is the precise readout; the dot just signals "alive."
  const cycleMs = $derived(
    state === 'active' ? 1200 :
    state === 'err'    ? 1600 :
    state === 'off'    ? 0    :  // no pulse when offline
    Math.max(6 * (ui.pingMs ?? 500), 3000)
  );

  const label = $derived(
    state === 'off'    ? 'off' :
    state === 'err'    ? 'err' :
    ui.pingMs == null  ? '…'   :
    `${ui.pingMs}ms`
  );
</script>

<span class="live" data-state={state} aria-label="link {state} {label}">
  <span
    class="dot"
    style:--cycle="{cycleMs}ms"
  ></span>
  <span class="label">{label}</span>
</span>

<style>
  .live {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 2px 8px 2px 7px;
    border: 1px solid var(--border);
    border-radius: 0;
    color: var(--text2);
    background: transparent;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 1px;
    line-height: 1;
    font-variant-numeric: tabular-nums;
    transition:
      color 180ms ease,
      border-color 180ms ease,
      background 180ms ease,
      box-shadow 180ms ease;
  }

  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
    display: inline-block;
    flex-shrink: 0;
    animation: pulse var(--cycle, 1000ms) ease-in-out infinite;
  }

  .label { display: inline-block; min-width: 36px; text-align: right; }

  /* OFF — disconnected, muted, no pulse. */
  .live[data-state="off"] {
    color: var(--text3);
    border-color: var(--text3);
    opacity: 0.7;
  }
  .live[data-state="off"] .dot { animation: none; opacity: 0.5; }

  /* ERR — flicker red. */
  .live[data-state="err"] {
    color: var(--danger);
    border-color: var(--danger);
    background: color-mix(in oklab, var(--danger) 8%, transparent);
  }
  .live[data-state="err"] .dot {
    animation: flicker var(--cycle, 800ms) steps(2, end) infinite;
  }

  /* OK — fast link, green pulse. */
  .live[data-state="ok"] {
    color: var(--ok);
    border-color: color-mix(in oklab, var(--ok) 55%, transparent);
    background: color-mix(in oklab, var(--ok) 8%, transparent);
  }

  /* SLOW — link up but ping > 300ms, warn pulse. */
  .live[data-state="slow"] {
    color: var(--warn);
    border-color: color-mix(in oklab, var(--warn) 60%, transparent);
    background: color-mix(in oklab, var(--warn) 8%, transparent);
  }

  /* ACTIVE — voice recording/transcribing. */
  .live[data-state="active"] {
    color: var(--recording);
    border-color: var(--recording);
    background: color-mix(in oklab, var(--recording) 14%, transparent);
    box-shadow:
      0 0 0 1px color-mix(in oklab, var(--recording) 30%, transparent),
      0 0 8px color-mix(in oklab, var(--recording) 45%, transparent);
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%      { opacity: 0.35; transform: scale(0.7); }
  }
  @keyframes flicker {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.3; }
  }

  @media (max-width: 720px) {
    .live { padding: 2px 6px 2px 5px; gap: 5px; }
    .dot { width: 6px; height: 6px; }
    .label { min-width: 32px; }
  }
</style>
