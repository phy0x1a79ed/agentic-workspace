<script lang="ts">
  interface Props {
    open: boolean;
    side?: 'left' | 'right';
    onclose?: () => void;
    children?: import('svelte').Snippet;
  }
  let { open = $bindable(false), side = 'left', onclose, children }: Props = $props();

  function close() {
    open = false;
    onclose?.();
  }
</script>

{#if open}
  <button class="scrim" aria-label="Close" onclick={close}></button>
  <aside class="sheet {side}" aria-modal="true" role="dialog">
    {@render children?.()}
  </aside>
{/if}

<style>
  .scrim {
    position: fixed;
    inset: 44px 0 0 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 40;
    border: 0;
    padding: 0;
    cursor: default;
  }
  .sheet {
    position: fixed;
    top: 44px;
    bottom: 52px;
    width: 86vw;
    max-width: 380px;
    background: var(--surface);
    z-index: 41;
    display: flex;
    flex-direction: column;
    box-shadow: 0 0 32px rgba(0, 0, 0, 0.55);
    overflow: hidden;
    animation: slideIn 0.18s cubic-bezier(0.2, 0.7, 0.2, 1);
  }
  .sheet.left  { left: 0;  border-right: 1px solid var(--border); }
  .sheet.right { right: 0; border-left:  1px solid var(--border); }
  .sheet.left  { animation-name: slideInL; }
  .sheet.right { animation-name: slideInR; }

  @keyframes slideInL { from { transform: translateX(-100%); } to { transform: none; } }
  @keyframes slideInR { from { transform: translateX( 100%); } to { transform: none; } }
  @keyframes slideIn  { from { opacity: 0; } to { opacity: 1; } }

  @media (min-width: 1025px) {
    .scrim, .sheet { display: none; }
  }
  @media (max-width: 720px) and (orientation: portrait) {
    .sheet { width: 92vw; max-width: 92vw; }
  }
</style>
