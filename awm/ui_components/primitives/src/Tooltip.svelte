<script lang="ts">
  /**
   * Themed tooltip — thin wrapper around `bits-ui`'s Tooltip. Pass `content`
   * (string), then provide a `trigger` snippet that receives `props` to spread
   * onto whatever element should hover-activate. Uses Select-menu chrome:
   * surface2 bg, border2 outline, mono 11px, --radius-md.
   */
  import { Tooltip as T } from 'bits-ui';
  import type { Snippet } from 'svelte';

  interface Props {
    content: string;
    side?: 'top' | 'right' | 'bottom' | 'left';
    delay?: number;
    trigger: Snippet<[{ props: Record<string, unknown> }]>;
  }
  let { content, side = 'top', delay = 250, trigger }: Props = $props();
</script>

<div style="display:contents" data-awm-component="Tooltip">
<T.Provider delayDuration={delay}>
  <T.Root>
    <T.Trigger>
      {#snippet child({ props })}
        {@render trigger({ props })}
      {/snippet}
    </T.Trigger>
    <T.Content {side} sideOffset={6}>
      <div class="ttip mono">{content}</div>
    </T.Content>
  </T.Root>
</T.Provider>
</div>

<style>
  .ttip {
    background: var(--surface2);
    border: 1px solid var(--border2);
    border-radius: var(--radius-md);
    color: var(--text);
    font-family: var(--mono);
    font-size: 11px;
    padding: var(--space-1) var(--space-3);
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.6);
    max-width: 320px;
    word-break: break-all;
    z-index: 50;
  }
</style>
