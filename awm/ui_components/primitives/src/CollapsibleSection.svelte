<script lang="ts">
  import type { Snippet } from 'svelte';
  import Card from './Card.svelte';
  import PanelLabel from './PanelLabel.svelte';

  interface Props {
    label?: string;
    rail?: 'manager' | 'peer' | 'plain' | 'warn' | 'none';
    flash?: boolean;
    open?: boolean;
    compact?: boolean;
    header?: Snippet;
    actions?: Snippet;
    children?: Snippet;
  }
  let {
    label,
    rail = 'plain',
    flash = false,
    open = $bindable(false),
    compact = false,
    header,
    actions,
    children,
  }: Props = $props();

  function toggle() { open = !open; }
</script>

<div style="display:contents" data-awm-component="CollapsibleSection">
<Card {rail} {flash} {open}>
  <header class="head" class:compact>
    <button
      class="hit"
      type="button"
      aria-expanded={open}
      onclick={toggle}
    >
      <span class="chev-glyph" class:open>▸</span>
      {#if header}
        {@render header()}
      {:else if label}
        <PanelLabel>{label}</PanelLabel>
      {/if}
    </button>
    {#if actions}
      <span class="actions">{@render actions()}</span>
    {/if}
  </header>

  {#if open}
    <div class="body" class:compact>
      {@render children?.()}
    </div>
  {/if}
</Card>
</div>

<style>
  .head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-height: 28px;
  }
  .head.compact { min-height: 22px; }
  .hit {
    flex: 1 1 auto;
    min-width: 0;
    background: transparent;
    border: 0;
    color: inherit;
    padding: var(--space-1) 0;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    text-align: left;
  }
  .chev-glyph {
    display: inline-block;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text3);
    transition: transform 160ms var(--ease-mech), color 140ms var(--ease-mech);
  }
  .chev-glyph.open { transform: rotate(90deg); color: var(--text2); }
  .hit:hover .chev-glyph { color: var(--text); }
  .actions { display: inline-flex; align-items: center; gap: var(--space-1); }

  .body {
    padding: var(--space-2) 0 var(--space-1) 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  .body.compact { padding: var(--space-1) 0; gap: var(--space-1); }
</style>
