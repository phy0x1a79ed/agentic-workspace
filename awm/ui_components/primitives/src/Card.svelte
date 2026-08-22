<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    rail?: 'manager' | 'peer' | 'plain' | 'warn' | 'none';
    flash?: boolean;
    open?: boolean;
    children?: Snippet;
  }
  let {
    rail = 'none',
    flash = false,
    open = false,
    children,
  }: Props = $props();
</script>

<article
  class="card r-{rail}"
  data-awm-component="Card"
  class:flash
  class:open
>{@render children?.()}</article>

<style>
  .card {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-left-width: 3px;
    border-radius: var(--radius-lg);
    padding: var(--space-2) var(--space-4) var(--space-2) var(--space-3);
    display: flex;
    flex-direction: column;
    transition: border-left-color 200ms var(--ease-mech),
                background 180ms var(--ease-mech);
  }
  .r-none    { border-left-width: 1px; }
  .r-manager { border-left-color: var(--atomizer); }
  .r-peer    { border-left-color: var(--recording); }
  .r-plain   { border-left-color: var(--text3); }
  .r-warn    { border-left-color: var(--warn); }

  .card.flash { border-left-color: var(--warn); }
  .card.open  { background: color-mix(in oklab, var(--surface3) 50%, var(--surface2)); }
</style>
