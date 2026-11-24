<script lang="ts">
  import { Tag } from '@awm/primitives';
  import type { GraphNode } from './api';

  let { node }: { node: GraphNode } = $props();
</script>

{#if node.kind === 'product'}
  <span class="n" title="files">{node.files}</span>
  {#if node.gaps?.length}<Tag tone="warn">{node.gaps.length} gaps</Tag>{/if}
{/if}
{#if node.stale && node.stale_ok}
  <Tag>stale ok</Tag>
{:else if node.stale}
  <Tag tone="danger">stale</Tag>
{:else if node.upstream_stale}
  <Tag tone="mgr">upstream</Tag>
{/if}

<style>
  .n { font-family: var(--mono); font-size: 10px; color: var(--text3); }
</style>
