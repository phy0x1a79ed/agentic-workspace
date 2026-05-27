<script lang="ts">
  /**
   * Transmission/oscilloscope indicator in the top nav. Color encodes
   * websocket health (`ui.wsKind`), waveform encodes voice activity
   * (`voice.connection` + `voice.stage`). Replaces WsDot + leader badge.
   */
  import { ui } from '$lib/state/ui.svelte';
  import { voice } from '$lib/state/voice.svelte';

  type State = 'off' | 'err' | 'live' | 'active';

  const state = $derived<State>(
    ui.wsKind === 'off' ? 'off' :
    ui.wsKind === 'err' ? 'err' :
    (voice.stage === 'recording' || voice.stage === 'transcribing') ? 'active' :
    'live'
  );

  // 48-unit-wide paths so a -12px (= 2 wavelengths of 6) translate gives a
  // seamless scroll inside the 36-unit viewBox.
  const PATHS = {
    flat:   'M0,8 L48,8',
    sine:   'M0,8 Q3,6 6,8 T12,8 T18,8 T24,8 T30,8 T36,8 T42,8 T48,8',
    thrash: 'M0,8 Q3,1 6,8 T12,8 T18,8 T24,8 T30,8 T36,8 T42,8 T48,8',
  } as const;

  const pathFor = $derived(
    state === 'live'   ? PATHS.sine   :
    state === 'active' ? PATHS.thrash :
                         PATHS.flat
  );
</script>

<span class="live" data-state={state} aria-label="live {state}">
  <svg class="wave" viewBox="0 0 36 16" preserveAspectRatio="none" aria-hidden="true">
    <path d={pathFor} />
  </svg>
  <span class="label">LIVE</span>
</span>

<style>
  .live {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 2px 7px 2px 5px;
    border: 1px solid var(--text3);
    border-radius: 0;
    color: var(--text3);
    background: transparent;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    line-height: 1;
    transition:
      color 180ms ease,
      border-color 180ms ease,
      background 180ms ease,
      box-shadow 180ms ease;
  }
  .wave {
    width: 36px;
    height: 12px;
    overflow: visible;
    display: block;
  }
  .wave path {
    fill: none;
    stroke: currentColor;
    stroke-width: 1.25;
    stroke-linecap: round;
    stroke-linejoin: round;
    transition: stroke-width 180ms ease;
  }
  .label { display: inline-block; }

  /* OFF — disconnected, flat, muted. */
  .live[data-state="off"] {
    color: var(--text3);
    border-color: var(--text3);
    opacity: 0.7;
  }

  /* ERR — flat + flicker, danger. */
  .live[data-state="err"] {
    color: var(--danger);
    border-color: var(--danger);
    background: color-mix(in oklab, var(--danger) 8%, transparent);
    animation: flicker 0.8s steps(2, end) infinite;
  }

  /* LIVE — ws connected, voice idle. Ambient sine. */
  .live[data-state="live"] {
    color: var(--ok);
    border-color: color-mix(in oklab, var(--ok) 55%, transparent);
    background: color-mix(in oklab, var(--ok) 8%, transparent);
  }
  .live[data-state="live"] .wave path {
    animation: scroll-slow 2.4s linear infinite;
  }

  /* ACTIVE — voice recording or transcribing. Fast thrash + glow. */
  .live[data-state="active"] {
    color: var(--recording);
    border-color: var(--recording);
    background: color-mix(in oklab, var(--recording) 14%, transparent);
    box-shadow:
      0 0 0 1px color-mix(in oklab, var(--recording) 30%, transparent),
      0 0 8px color-mix(in oklab, var(--recording) 45%, transparent);
  }
  .live[data-state="active"] .wave path {
    stroke-width: 1.5;
    animation: scroll-fast 0.4s linear infinite;
  }

  @keyframes scroll-slow {
    from { transform: translateX(0); }
    to   { transform: translateX(-12px); }
  }
  @keyframes scroll-fast {
    from { transform: translateX(0); }
    to   { transform: translateX(-12px); }
  }
  @keyframes flicker {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.4; }
  }

  @media (max-width: 720px) {
    .live { padding: 2px 6px 2px 4px; gap: 5px; }
    .wave { width: 28px; }
  }
</style>
