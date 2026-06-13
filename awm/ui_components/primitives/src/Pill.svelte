<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    tone?: 'neutral' | 'atomizer' | 'danger';
    disabled?: boolean;
    onclick?: (e: MouseEvent) => void;
    title?: string;
    type?: 'button' | 'submit';
    'aria-pressed'?: boolean | 'true' | 'false';
    children?: Snippet;
  }
  let {
    tone = 'neutral',
    disabled = false,
    onclick,
    title,
    type = 'button',
    'aria-pressed': ariaPressed,
    children,
  }: Props = $props();
</script>

<button
  class="pill t-{tone}"
  {type}
  {disabled}
  {onclick}
  {title}
  aria-pressed={ariaPressed}
>{@render children?.()}</button>

<style>
  .pill {
    background: var(--surface3);
    color: var(--text2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: var(--space-0) var(--space-3);
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 120ms var(--ease-mech),
                border-color 120ms var(--ease-mech),
                color 120ms var(--ease-mech);
  }
  .pill:disabled { opacity: 0.5; cursor: default; }

  .t-neutral:hover:not(:disabled) {
    background: color-mix(in oklab, var(--atomizer) 18%, var(--surface3));
    border-color: color-mix(in oklab, var(--atomizer) 45%, var(--border));
    color: var(--text);
  }

  .t-atomizer {
    color: var(--atomizer);
    border-color: color-mix(in oklab, var(--atomizer) 45%, var(--border));
    background: color-mix(in oklab, var(--atomizer) 14%, var(--surface3));
  }
  .t-atomizer:hover:not(:disabled) {
    background: color-mix(in oklab, var(--atomizer) 24%, var(--surface3));
  }

  .t-danger {
    color: var(--danger);
    border-color: color-mix(in oklab, var(--danger) 40%, var(--border));
  }
  .t-danger:hover:not(:disabled) {
    background: color-mix(in oklab, var(--danger) 14%, var(--surface3));
    border-color: color-mix(in oklab, var(--danger) 50%, var(--border));
  }

  @media (max-width: 720px) {
    .pill { padding: 5px var(--space-3); }
  }
</style>
