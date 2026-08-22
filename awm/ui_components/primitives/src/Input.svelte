<script lang="ts">
  interface Props {
    value: string;
    placeholder?: string;
    type?: 'text' | 'search';
    disabled?: boolean;
    autocomplete?: HTMLInputElement['autocomplete'];
    oninput?: (e: Event) => void;
    onkeydown?: (e: KeyboardEvent) => void;
    onblur?: (e: FocusEvent) => void;
    onfocus?: (e: FocusEvent) => void;
  }
  let {
    value = $bindable(),
    placeholder,
    type = 'text',
    disabled = false,
    autocomplete,
    oninput,
    onkeydown,
    onblur,
    onfocus,
  }: Props = $props();

  let el: HTMLInputElement | undefined = $state();
  export function focus() { el?.focus(); }
  export function el_(): HTMLInputElement | undefined { return el; }
</script>

<input
  bind:this={el}
  bind:value
  class="input"
  data-awm-component="Input"
  {type}
  {placeholder}
  {disabled}
  {autocomplete}
  {oninput}
  {onkeydown}
  {onblur}
  {onfocus}
/>

<style>
  .input {
    width: 100%;
    padding: var(--space-2) var(--space-4);
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    outline: none;
    transition: border-color 120ms var(--ease-mech);
  }
  .input:focus { border-color: var(--atomizer); }
  .input::placeholder { color: var(--text3); }

  @media (max-width: 720px) {
    .input { min-height: 44px; font-size: 16px; }
  }
</style>
