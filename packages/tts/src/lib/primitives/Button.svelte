<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    kind?: 'primary' | 'ghost' | 'danger';
    size?: 'sm' | 'md';
    type?: 'button' | 'submit' | 'reset';
    disabled?: boolean;
    onclick?: (e: MouseEvent) => void;
    title?: string;
    'aria-label'?: string;
    children?: Snippet;
  }
  let {
    kind = 'ghost',
    size = 'md',
    type = 'button',
    disabled = false,
    onclick,
    title,
    'aria-label': ariaLabel,
    children,
  }: Props = $props();
</script>

<button
  class="btn k-{kind} s-{size}"
  {type}
  {disabled}
  {onclick}
  {title}
  aria-label={ariaLabel}
>{@render children?.()}</button>

<style>
  .btn {
    border: 1px solid var(--border);
    color: var(--text);
    font-family: var(--mono);
    cursor: pointer;
    transition: background 120ms var(--ease-mech),
                border-color 120ms var(--ease-mech),
                color 120ms var(--ease-mech);
  }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  /* size */
  .s-md {
    padding: var(--space-2) 12px;
    font-size: 11px;
    letter-spacing: 0.5px;
    border-radius: var(--radius-lg);
    background: var(--surface2);
  }
  .s-sm {
    padding: var(--space-1) 12px;
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
    border-radius: var(--radius-md);
    background: var(--surface3);
    color: var(--text2);
  }

  /* kind — md */
  .s-md.k-ghost   { background: transparent; }
  .s-md.k-ghost:hover:not(:disabled)  { background: var(--surface2); }
  .s-md.k-primary {
    background: color-mix(in oklab, var(--atomizer) 18%, var(--surface2));
    border-color: color-mix(in oklab, var(--atomizer) 50%, var(--border));
  }
  .s-md.k-primary:hover:not(:disabled) {
    background: color-mix(in oklab, var(--atomizer) 28%, var(--surface2));
  }
  .s-md.k-danger {
    color: var(--danger);
    border-color: color-mix(in oklab, var(--danger) 40%, var(--border));
  }
  .s-md.k-danger:hover:not(:disabled) {
    background: color-mix(in oklab, var(--danger) 12%, var(--surface2));
  }
  .s-md:hover:not(:disabled) { border-color: var(--border2); }

  /* kind — sm (uppercase chip) */
  .s-sm.k-ghost:hover:not(:disabled) {
    background: color-mix(in oklab, var(--atomizer) 18%, var(--surface3));
    border-color: color-mix(in oklab, var(--atomizer) 50%, var(--border));
    color: var(--text);
  }
  .s-sm.k-primary {
    background: color-mix(in oklab, var(--atomizer) 22%, var(--surface3));
    border-color: color-mix(in oklab, var(--atomizer) 55%, var(--border));
    color: var(--text);
  }
  .s-sm.k-danger {
    color: var(--danger);
    border-color: color-mix(in oklab, var(--danger) 40%, var(--border));
  }
  .s-sm.k-danger:hover:not(:disabled) {
    background: color-mix(in oklab, var(--danger) 18%, var(--surface3));
    border-color: color-mix(in oklab, var(--danger) 50%, var(--border));
  }

  @media (max-width: 720px) {
    .s-md { min-height: 44px; padding: var(--space-4) var(--space-5); font-size: 12px; }
  }
</style>
