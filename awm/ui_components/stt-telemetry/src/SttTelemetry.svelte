<script lang="ts">
  import type { TelemetryEvent } from './types';

  // DEV telemetry timeline: a scrollable, high-resolution log of every STT
  // pipeline event with the delta since the previous event and — for timed
  // backend stages — how long it took. Purely presentational: it owns no socket
  // and no store. The parent feeds `events` (already coalesced + capped by
  // `appendTelemetry`) and owns Clear via `onclear`.
  //
  // Rows are colored by direction: outbound (browser→server), inbound (a normal
  // down-frame), backend (a dev `metric` frame carrying a server-measured
  // duration). See ./types for the event shape.

  interface Props {
    events: TelemetryEvent[];
    onclear?: () => void;
  }
  let { events, onclear }: Props = $props();

  function fmtTime(ms: number): string {
    const total = ms / 1000;
    const m = Math.floor(total / 60);
    const s = total - m * 60;
    return `${String(m).padStart(2, '0')}:${s.toFixed(3).padStart(6, '0')}`;
  }

  // Decorate each event with a display time (relative to the first row) and the
  // Δ from the previous row. Recomputed when `events` changes.
  const rows = $derived.by(() => {
    const base = events.length ? events[0].t : 0;
    let prev: number | null = null;
    return events.map((e) => {
      const delta = prev === null ? null : e.t - prev;
      prev = e.t;
      return { ...e, time: fmtTime(e.t - base), delta };
    });
  });

  let scroller: HTMLDivElement | null = $state(null);
  // Pin to the newest row, but release the pin while the user scrolls up to
  // inspect history so the log doesn't yank them back down.
  let pinned = $state(true);
  function onScroll() {
    if (!scroller) return;
    pinned = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 24;
  }
  $effect(() => {
    rows.length; // track
    if (!pinned) return;
    queueMicrotask(() => {
      if (scroller) scroller.scrollTop = scroller.scrollHeight;
    });
  });
</script>

<div class="telemetry" data-awm-component="SttTelemetry">
  <div class="head">
    <span class="title">timeline</span>
    <span class="count">{events.length}</span>
    {#if !pinned}<span class="frozen" title="autoscroll paused — scroll to bottom to resume">paused</span>{/if}
    <button
      type="button"
      class="clear"
      onclick={() => onclear?.()}
      disabled={events.length === 0}
      aria-label="clear telemetry log"
    >CLEAR</button>
  </div>

  <div bind:this={scroller} class="log" role="log" aria-label="stt event telemetry" onscroll={onScroll}>
    {#if rows.length === 0}
      <p class="empty">no events yet — start talking to capture the pipeline.</p>
    {:else}
      {#each rows as row, i (i)}
        <div class="row" data-dir={row.dir}>
          <span class="t">{row.time}</span>
          <span class="d">{row.delta === null ? '—' : `+${row.delta.toFixed(1)}`}</span>
          <span class="ev">{row.event}</span>
          <span class="took">{row.dur === undefined ? '' : `${row.dur}ms`}</span>
          <span class="detail">{row.detail ?? ''}</span>
        </div>
      {/each}
    {/if}
  </div>
</div>

<style>
  .telemetry {
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
    width: 100%;
    min-width: 300px;
  }
  .head {
    display: flex;
    align-items: center;
    gap: var(--space-3, 12px);
    font-family: var(--mono, monospace);
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: var(--text3, #888);
  }
  .title { color: var(--text2, #bbb); }
  .count {
    color: var(--text, #ddd);
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-sm, 2px);
    padding: 0 6px;
  }
  .frozen { color: var(--atomizer, #ffb74d); }
  .clear {
    margin-left: auto;
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-sm, 2px);
    color: var(--text2, #bbb);
    font-family: var(--mono, monospace);
    font-size: 10px;
    letter-spacing: 1px;
    padding: 2px 8px;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .clear:hover:not(:disabled) { background: var(--surface3, #2a2a2a); color: var(--text, #ddd); }
  .clear:disabled { opacity: 0.35; cursor: not-allowed; }

  .log {
    min-height: 160px;
    max-height: 320px;
    overflow-y: auto;
    overflow-x: hidden;
    padding: var(--space-2, 8px);
    background: var(--bg, #111);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 4px);
    font-family: var(--mono, monospace);
    font-size: 11px;
    line-height: 1.5;
  }
  .empty {
    color: var(--text3, #888);
    font-style: italic;
    margin: 0;
  }
  .row {
    display: grid;
    /* fixed-width time/Δ/took columns so rows don't jitter as values change */
    grid-template-columns: 5.5em 5em 9em 5em 1fr;
    gap: 8px;
    white-space: nowrap;
  }
  .row:hover { background: var(--surface, #17171a); }
  .t { color: var(--text3, #888); }
  .d { color: var(--text3, #888); text-align: right; }
  .took { color: var(--text2, #bbb); text-align: right; }
  .detail {
    color: var(--text3, #888);
    overflow: hidden;
    text-overflow: ellipsis;
  }
  /* event-name color carries the direction */
  .ev { color: var(--text2, #bbb); }
  .row[data-dir='out'] .ev { color: var(--recording, #a855f7); }
  .row[data-dir='in'] .ev { color: var(--text, #ddd); }
  .row[data-dir='backend'] .ev { color: var(--atomizer, #ffb74d); }
</style>
