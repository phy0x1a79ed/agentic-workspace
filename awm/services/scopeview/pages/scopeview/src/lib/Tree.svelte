<script lang="ts">
  import { SvelteSet } from 'svelte/reactivity';
  import Badges from './Badges.svelte';
  import type { TreeItem } from './tree';

  interface Props {
    items: TreeItem[];
    selected: string;
    onselect: (id: string) => void;
  }
  let { items, selected, onselect }: Props = $props();

  const flipped = new SvelteSet<string>();
  const toggle = (key: string) => (flipped.has(key) ? flipped.delete(key) : flipped.add(key));
</script>

{#snippet rows(list: TreeItem[], depth: number)}
  {#each list as item (item.key)}
    {@const n = item.node}
    {@const open = flipped.has(item.key) === !!item.closed}
    <div
      class="row"
      class:group={!n}
      class:top={depth === 0}
      class:link={item.link}
      class:selected={n?.id === selected}
      data-stage={item.stage}
      style:padding-left="{6 + depth * 14}px"
    >
      {#if item.children.length}
        <button class="twist" onclick={() => toggle(item.key)} aria-label={open ? 'collapse' : 'expand'}>
          {open ? '▾' : '▸'}
        </button>
      {:else}
        <span class="twist"></span>
      {/if}
      {#if n}
        <button class="pick" onclick={() => onselect(n.id)} title={item.link ? `${n.id} — shown under each node built from it` : n.id}>
          {#if item.link}
            <span class="mark">↗</span>
          {:else}
            <span class="mark" title={n.status ?? n.kind}>{n.status === 'accepted' ? '●' : n.status ? '○' : '▪'}</span>
          {/if}
          <span class="label">{item.label}</span>
          <Badges node={n} />
        </button>
      {:else}
        <button class="pick" onclick={() => toggle(item.key)}>
          <span class="label">{item.label}</span>
          <span class="count">{item.count}</span>
        </button>
      {/if}
    </div>
    {#if open}{@render rows(item.children, depth + 1)}{/if}
  {/each}
{/snippet}

<nav>{@render rows(items, 0)}</nav>

<style>
  nav { padding: var(--space-2) 0; }
  .row { display: flex; align-items: center; min-height: 24px; padding-right: var(--space-3); }
  .row:hover { background: var(--surface2); }
  .row.selected { background: var(--surface3); box-shadow: inset 2px 0 0 var(--stage); }
  button { all: unset; cursor: pointer; }
  button:focus-visible { outline: 1px solid var(--atomizer); }
  .twist { width: 16px; flex: none; text-align: center; color: var(--text3); font-size: 10px; }
  .pick { display: flex; align-items: center; gap: var(--space-2); flex: 1; min-width: 0; padding: var(--space-0) 0; }
  .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 0 1 auto; }
  .mark { color: var(--stage); font-size: 10px; flex: none; width: 10px; text-align: center; }
  .group .label { font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--text2); }
  .top .label { color: var(--stage); }
  .count { font-family: var(--mono); font-size: 10px; color: var(--text3); }
  .link .label { color: var(--text2); font-size: 12px; }
</style>
