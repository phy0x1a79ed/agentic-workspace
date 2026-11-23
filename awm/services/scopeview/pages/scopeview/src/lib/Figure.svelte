<script lang="ts">
  import type { PlotlyFigure } from './api';

  let { figure }: { figure: PlotlyFigure } = $props();

  let el: HTMLDivElement;
  let error = $state<string | null>(null);
  let drawn = $state(false);

  $effect(() => {
    const fig = figure;
    const target = el;
    let live = true;
    let plotly: typeof import('plotly.js-dist-min').default | undefined;
    let sized: ResizeObserver | undefined;
    import('plotly.js-dist-min')
      .then(async (m) => {
        if (!live) return;
        plotly = m.default;
        const { width: _fixed, ...layout } = fig.layout ?? {};
        await plotly.react(target, fig.data, { ...layout, autosize: true }, { responsive: true, displaylogo: false });
        drawn = true;
        sized = new ResizeObserver(() => plotly?.Plots.resize(target));
        sized.observe(target);
      })
      .catch((e) => (error = e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
      sized?.disconnect();
      plotly?.purge(target);
    };
  });
</script>

{#if error}<p class="error">figure failed to draw: {error}</p>{/if}
{#if !drawn && !error}<p class="wait">drawing…</p>{/if}
<div class="plot" bind:this={el}></div>

<style>
  .plot { width: 100%; min-height: 460px; background: #fff; border-radius: var(--radius-lg); overflow: hidden; }
  .wait { color: var(--text2); margin: 0 0 var(--space-2); }
  .error { color: var(--danger); }
</style>
