<script lang="ts">
  import type { PlotlyFigure } from './api';
  import { theme } from './theme.svelte';

  interface Props {
    figure: PlotlyFigure;
    /** A mark's customdata as the pointer reaches it (null as it leaves), and `pin` when it is clicked. */
    onmark?: (customdata: unknown, pin: boolean) => void;
  }
  let { figure, onmark }: Props = $props();

  type Plotly = typeof import('plotly.js-dist-min').default;
  let el: HTMLDivElement;
  let plotly = $state.raw<Plotly | null>(null);
  let error = $state<string | null>(null);
  let drawn = $state(false);

  const fail = (e: unknown) => (error = e instanceof Error ? e.message : String(e));
  const sub = (v: unknown) => (v && typeof v === 'object' ? (v as Record<string, unknown>) : {});

  /** Dark only: the figure's ground and ink become the panel's. Trace colours stay the figure's own. */
  function themed(layout: Record<string, unknown>): Record<string, unknown> {
    if (theme.current !== 'dark') return layout;
    const css = getComputedStyle(el);
    const ground = css.getPropertyValue('--bg').trim();
    const rule = css.getPropertyValue('--border2').trim();
    const axes = new Set(['xaxis', 'yaxis', ...Object.keys(layout).filter((k) => /^[xy]axis\d*$/.test(k))]);
    return {
      ...layout,
      paper_bgcolor: ground,
      plot_bgcolor: ground,
      font: { ...sub(layout.font), color: css.getPropertyValue('--text').trim() },
      ...Object.fromEntries(
        [...axes].map((k) => [k, { ...sub(layout[k]), gridcolor: rule, zerolinecolor: rule, linecolor: rule }]),
      ),
    };
  }

  $effect(() => {
    const target = el;
    let loaded: Plotly | undefined;
    const sized = new ResizeObserver(() => drawn && loaded?.Plots.resize(target));
    sized.observe(target);
    import('plotly.js-dist-min').then((m) => (plotly = loaded = m.default)).catch(fail);
    return () => {
      sized.disconnect();
      loaded?.purge(target);
    };
  });

  $effect(() => {
    if (!plotly) return;
    const { width: _fixed, ...layout } = figure.layout ?? {};
    plotly
      .react(el, figure.data, { ...themed(layout), autosize: true, uirevision: 'kept' }, { responsive: true, displaylogo: false })
      .then(() => {
        if (!drawn) {
          const marks = el as unknown as { on(event: string, f: (e: { points: { customdata?: unknown }[] }) => void): void };
          marks.on('plotly_hover', (e) => onmark?.(e.points[0]?.customdata, false));
          marks.on('plotly_unhover', () => onmark?.(null, false));
          marks.on('plotly_click', (e) => onmark?.(e.points[0]?.customdata, true));
        }
        drawn = true;
        error = null;
      })
      .catch(fail);
  });
</script>

{#if error}<p class="error">figure failed to draw: {error}</p>{/if}
{#if !drawn && !error}<p class="wait">drawing…</p>{/if}
<div class="plot" bind:this={el}></div>

<style>
  .plot { width: 100%; min-height: 460px; border-radius: var(--radius-lg); overflow: hidden; }
  .wait { color: var(--text2); margin: 0 0 var(--space-2); }
  .error { color: var(--danger); }
</style>
