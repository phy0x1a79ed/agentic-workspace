<script lang="ts">
  /**
   * Shared full-screen page shell for the fleet's secondary surfaces (new-agent,
   * settings), so they read as pages — like the terminal overlay — rather than
   * centered modals. Mirrors @awm/terminal's FleetTerminal chrome: a fixed
   * inset-0 frame with a back/title header, a scrolling body, and an optional
   * pinned footer (safe-area aware) for the action row.
   */
  import type { Snippet } from 'svelte';

  interface Props {
    title: string;
    subtitle?: string;
    onClose: () => void;
    children: Snippet;
    footer?: Snippet;
  }
  let { title, subtitle = '', onClose, children, footer }: Props = $props();
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape') onClose(); }} />
<div class="fleet-sheet">
  <header class="fs-head">
    <button class="fs-back" onclick={onClose} aria-label="back">‹</button>
    <div class="fs-title">
      <span class="fs-name">{title}</span>
      {#if subtitle}<span class="fs-sub">{subtitle}</span>{/if}
    </div>
  </header>

  <div class="fs-body">
    {@render children()}
  </div>

  {#if footer}
    <div class="fs-footer">
      {@render footer()}
    </div>
  {/if}
</div>

<style>
  /* Frame + header intentionally match @awm/terminal's FleetTerminal so the two
     surfaces feel identical. */
  .fleet-sheet {
    position: fixed;
    inset: 0;
    z-index: 50;
    display: flex;
    flex-direction: column;
    background: #101010;
    color: var(--text, #d6d6d6);
  }
  .fs-head {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    background: var(--surface2, #181818);
    border-bottom: 1px solid var(--border, #2a2a2a);
  }
  .fs-back {
    font-size: 1.4rem;
    line-height: 1;
    background: none;
    border: none;
    color: var(--text, #ddd);
    padding: 0 6px;
    cursor: pointer;
  }
  .fs-title { display: flex; flex-direction: column; min-width: 0; flex: 1 1 auto; }
  .fs-name { font-weight: 600; font-size: 0.95rem; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .fs-sub { font-size: 0.72rem; color: var(--text3, #888); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }

  .fs-body {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    max-width: 560px;
    width: 100%;
    margin: 0 auto;
  }

  .fs-footer {
    flex: 0 0 auto;
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    padding: 10px 16px calc(10px + env(safe-area-inset-bottom, 0px));
    background: var(--surface2, #181818);
    border-top: 1px solid var(--border, #2a2a2a);
  }
</style>
