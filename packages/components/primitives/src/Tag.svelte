<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    tone?: 'neutral' | 'ok' | 'warn' | 'danger' | 'atomizer' | 'mgr';
    children?: Snippet;
  }
  let { tone = 'neutral', children }: Props = $props();
</script>

<span class="tag t-{tone}">{@render children?.()}</span>

<style>
  .tag {
    font-family: var(--mono);
    font-size: 8px;
    letter-spacing: 1px;
    padding: var(--space-0) var(--space-3);
    border-radius: var(--radius-md);
    text-transform: uppercase;
    border: 1px solid;
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    white-space: nowrap;
  }
  .t-neutral  { color: var(--text3);    border-color: var(--text3); }
  .t-ok       { color: var(--ok);       border-color: color-mix(in oklab, var(--ok)       40%, transparent); background: color-mix(in oklab, var(--ok)       10%, transparent); }
  .t-warn     { color: var(--warn);     border-color: color-mix(in oklab, var(--warn)     40%, transparent); background: color-mix(in oklab, var(--warn)     10%, transparent); }
  .t-danger   { color: var(--danger);   border-color: color-mix(in oklab, var(--danger)   40%, transparent); background: color-mix(in oklab, var(--danger)   10%, transparent); }
  .t-atomizer { color: var(--atomizer); border-color: color-mix(in oklab, var(--atomizer) 40%, transparent); background: color-mix(in oklab, var(--atomizer) 10%, transparent); }
  .t-mgr      { color: var(--mgr);      border-color: color-mix(in oklab, var(--mgr)      40%, transparent); background: color-mix(in oklab, var(--mgr)      10%, transparent); }
</style>
