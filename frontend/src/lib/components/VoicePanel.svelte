<script lang="ts">
  /**
   * Thin presentational layer over the `voice` store. Renders the connection
   * dot, the stage label (idle / recording / transcribing / error), and a VU
   * meter that reflects the current mic RMS. Has no buttons — PttButton is
   * the trigger; this is the readout.
   *
   * Auto-connects on mount and disconnects on destroy; the focus page mounts
   * one instance for the whole tab.
   */
  import { onMount, onDestroy } from 'svelte';
  import { voice } from '$lib/state/voice.svelte';

  onMount(() => { voice.connect(); });
  onDestroy(() => { voice.disconnect(); });
</script>

<div class="vp mono" data-stage={voice.stage} data-conn={voice.connection}>
  <span class="dot" title={voice.connection}></span>
  <span class="stage">{voice.stageText}</span>
  <span class="meter" aria-hidden="true">
    <span class="bar" style:width="{Math.round(voice.meter * 100)}%"></span>
  </span>
</div>

<style>
  .vp {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 4px 10px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: var(--text3);
    min-height: 24px;
  }
  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--text3);
    flex-shrink: 0;
  }
  .vp[data-conn="connected"] .dot { background: var(--ok, #10b981); }
  .vp[data-conn="connecting"] .dot { background: var(--text2); }

  .stage { color: var(--text2); }
  .vp[data-stage="recording"]    .stage { color: var(--recording, #ef4444); }
  .vp[data-stage="transcribing"] .stage { color: var(--atomizer, #3b82f6); }
  .vp[data-stage="error"]        .stage { color: var(--err, #ef4444); }

  .meter {
    display: inline-block;
    width: 60px;
    height: 4px;
    background: var(--surface3);
    border-radius: 2px;
    overflow: hidden;
    flex-shrink: 0;
  }
  .bar {
    display: block;
    height: 100%;
    background: var(--atomizer, #3b82f6);
    transition: width 0.05s linear;
  }
  .vp[data-stage="recording"] .bar { background: var(--recording, #ef4444); }
</style>
