<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    tone?: 'dim' | 'atomizer' | 'mgr' | 'peer';
    children?: Snippet;
  }
  let { tone = 'dim', children }: Props = $props();
</script>

<span class="lbl t-{tone}" data-awm-component="PanelLabel">{@render children?.()}</span>

<style>
  .lbl {
    font-family: var(--mono);
    font-size: 9px;
    letter-spacing: 2px;
    text-transform: uppercase;
    font-weight: 500;
  }
  .t-dim      { color: var(--text3); }
  .t-atomizer { color: var(--atomizer); }
  .t-mgr      { color: var(--mgr); }
  .t-peer     { color: var(--recording); }
</style>
