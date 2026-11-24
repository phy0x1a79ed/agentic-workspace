<script lang="ts">
  /**
   * Metasmith's DAG drawing: markers and routed edges in an SVG, with one button per row laid
   * over it. Placement and every edge path arrive finished from the `layout` verb.
   */
  import type { Layout } from './api';

  interface Props {
    geo: Layout;
    /** Nodes to light besides the focus. An edge lights when both its ends do. */
    highlighted?: ReadonlySet<string>;
    title?: (id: string) => string;
    onpick?: (id: string) => void;
  }
  let { geo, highlighted, title, onpick }: Props = $props();

  const lit = (id: string) => !!highlighted?.has(id);
</script>

<div class="rail" class:tracing={!!highlighted} style:width="{geo.width}px" style:height="{geo.height}px">
  <svg width={geo.width} height={geo.height} aria-hidden="true">
    <g fill="none" stroke-linejoin="round" stroke-linecap="round">
      {#each geo.edges as e (`${e.from} ${e.to}`)}
        {#if !e.back}<path class="edge" class:lit={lit(e.from) && lit(e.to)} d={e.d} />{/if}
      {/each}
    </g>
    {#each geo.nodes as n (n.id)}
      {@const w = n.marker_w}
      {@const h = n.marker_h}
      <g class="mk {n.kind}" class:focus={n.focus} class:lit={lit(n.id)} class:stale={n.stale}>
        {#if n.kind === 'transform'}
          <polygon
            points="{n.cx - w / 2},{n.cy - h / 2} {n.cx + w / 2},{n.cy - h / 2} {n.cx},{n.cy + h / 2}"
            stroke-linejoin="round"
          />
        {:else}
          <circle cx={n.cx} cy={n.cy} r={w / 2} />
        {/if}
      </g>
    {/each}
  </svg>

  {#each geo.nodes as n (n.id)}
    {@const left = n.cx - n.marker_w / 2}
    <button
      class="node {n.kind}"
      class:focus={n.focus}
      class:lit={lit(n.id)}
      data-stage={n.stage}
      style:left="{left}px"
      style:top="{n.cy - geo.row_pitch / 2}px"
      style:width="{geo.width - left}px"
      style:height="{geo.row_pitch}px"
      style:font-size="{geo.font_size}px"
      title={title?.(n.id) ?? n.id}
      aria-current={n.focus ? 'true' : undefined}
      onclick={() => onpick?.(n.id)}
    >
      <span class="text" style:margin-left="{n.label_x - left}px">
        <span class="label">{n.label}</span>
        {#if n.stale}<span class="flag">stale</span>{/if}
      </span>
    </button>
  {/each}
</div>

<style>
  .rail { position: relative; flex: none; }
  svg { position: absolute; top: 0; left: 0; overflow: visible; }

  .edge { stroke: var(--dag-edge); stroke-width: 1.4; stroke-opacity: 0.55; }
  .edge.lit { stroke: var(--atomizer); stroke-opacity: 1; stroke-width: 2.6; }
  .tracing .edge:not(.lit) { stroke-opacity: 0.15; }
  .tracing .mk:not(.lit):not(.focus), .tracing .node:not(.lit):not(.focus) { opacity: 0.3; }

  /* A hollow marker is filled with the ground it sits on, so it reads hollow in either theme. */
  .mk { fill: var(--dag-ground, var(--surface)); stroke: var(--dag-data); stroke-width: 1.3; }
  .mk.transform { stroke: var(--dag-step); stroke-width: 2; }
  .mk.given, .mk.target { fill: var(--dag-solid); stroke: var(--dag-solid-stroke); }
  .mk.stale { stroke: var(--danger); }
  .mk.lit { stroke: var(--atomizer); stroke-width: 2.2; filter: drop-shadow(0 0 4px var(--atomizer)); }
  .mk.focus { filter: drop-shadow(0 0 4px var(--atomizer)) drop-shadow(0 0 2px var(--atomizer)); }

  /* A row runs from its marker to the right edge so the whole line picks. It stays transparent
     because the lanes to its right pass underneath, and the highlight sits on the text alone. */
  .node { all: unset; position: absolute; display: flex; align-items: center; overflow: hidden; cursor: pointer; box-sizing: border-box; }
  .text { display: flex; align-items: baseline; gap: var(--space-2); min-width: 0; padding: 1px var(--space-1); border-radius: var(--radius-md); line-height: 1.15; }
  .label { font-family: var(--mono); color: var(--dag-data); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .transform .label { color: var(--dag-step); font-weight: 600; }
  .flag { font-family: var(--mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: var(--danger); }
  .node:hover .text, .node.lit .text, .node.focus .text { background: var(--surface2); }
  .node.focus .text { box-shadow: inset 2px 0 0 var(--stage); }
  .node.focus .label { color: var(--text); }
  .node.lit .text { background: var(--trace-pin); outline: 1px solid var(--atomizer); outline-offset: -1px; }
  .node.lit .label { color: var(--text); font-weight: 600; }
  .node:focus-visible .text { outline: 1px solid var(--atomizer); }
</style>
