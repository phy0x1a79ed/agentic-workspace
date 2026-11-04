<script lang="ts">
  // ─────────────────────────────────────────────────────────────────────
  // The dashboard's ONE placeholder component.
  //
  // Every region that will eventually hold a real component renders a
  // Skeleton labeled with the name of that future component (e.g.
  // "status-table", "conversable-agents", "task-card"). It is layout
  // scaffolding — not a shipped UI primitive. When a real component lands
  // (built by another scope), it replaces the Skeleton at that spot.
  // ─────────────────────────────────────────────────────────────────────

  type Variant = 'block' | 'line' | 'card' | 'avatar';

  let {
    label = '',
    variant = 'block',
    lines = 1,
    height = ''
  }: {
    label?: string;
    variant?: Variant;
    lines?: number;
    height?: string;
  } = $props();
</script>

{#if variant === 'line'}
  <div class="sk-lines" aria-hidden="true">
    {#each Array(lines) as _, i (i)}
      <span class="sk-line" style:width={i === lines - 1 ? '60%' : '100%'}></span>
    {/each}
  </div>
{:else if variant === 'avatar'}
  <span class="sk-avatar" aria-hidden="true"></span>
{:else}
  <div
    class="sk {variant}"
    style:height={height || (variant === 'card' ? '72px' : '')}
    role="img"
    aria-label={label ? `placeholder: ${label}` : 'placeholder'}
  >
    {#if label}<span class="sk-label">{label}</span>{/if}
  </div>
{/if}

<style>
  .sk {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    min-height: 40px;
    border: 1px dashed var(--border2);
    border-radius: var(--radius-md);
    background:
      repeating-linear-gradient(
        -45deg,
        var(--surface2),
        var(--surface2) 8px,
        var(--surface) 8px,
        var(--surface) 16px
      );
    color: var(--text3);
  }
  .sk.card {
    align-items: flex-start;
    padding: var(--space-3);
  }
  .sk-label {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text3);
    padding: var(--space-0) var(--space-2);
    background: color-mix(in oklab, var(--bg) 60%, transparent);
    border-radius: var(--radius-sm);
  }

  .sk-lines {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    width: 100%;
  }
  .sk-line {
    height: 8px;
    border-radius: var(--radius-sm);
    background: var(--surface3);
  }

  .sk-avatar {
    display: inline-block;
    width: 26px;
    height: 26px;
    border-radius: 50%;
    background: var(--surface3);
    border: 1px solid var(--border2);
    flex: none;
  }
</style>
