<script lang="ts">
  /**
   * Themed dropdown — replacement for native `<select>` whose popup list
   * is OS-rendered and unstyleable. Renders a button that mirrors the
   * AgentControls underline-input look, and on click opens an absolutely
   * positioned mono list using the surface/border tokens. Click-outside
   * + Escape closes it; Enter/Space toggles; Up/Down + Enter navigates.
   */
  import { onMount } from 'svelte';

  interface Props {
    value: string;
    options: readonly string[];
    onchange?: (v: string) => void;
    disabled?: boolean;
    placeholder?: string;
  }
  let { value, options, onchange, disabled = false, placeholder = '' }: Props = $props();

  let open = $state(false);
  let highlight = $state<number>(-1);
  let root: HTMLDivElement;

  function toggle() {
    if (disabled) return;
    open = !open;
    highlight = open ? Math.max(0, options.indexOf(value)) : -1;
  }
  function close() { open = false; highlight = -1; }
  function pick(v: string) {
    if (v !== value) onchange?.(v);
    close();
  }

  function onKey(e: KeyboardEvent) {
    if (disabled) return;
    if (!open) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
        e.preventDefault(); toggle();
      }
      return;
    }
    if (e.key === 'Escape')      { e.preventDefault(); close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); highlight = (highlight + 1) % options.length; }
    else if (e.key === 'ArrowUp')   { e.preventDefault(); highlight = (highlight - 1 + options.length) % options.length; }
    else if (e.key === 'Enter')     { e.preventDefault(); if (highlight >= 0) pick(options[highlight]); }
  }

  onMount(() => {
    const onDocClick = (e: MouseEvent) => {
      if (open && root && !root.contains(e.target as Node)) close();
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  });
</script>

<div class="sel" bind:this={root}>
  <button
    type="button"
    class="trigger mono"
    class:open
    {disabled}
    aria-haspopup="listbox"
    aria-expanded={open}
    onclick={toggle}
    onkeydown={onKey}
  >
    <span class="val" class:placeholder={!value}>{value || placeholder}</span>
    <span class="caret" aria-hidden="true">▾</span>
  </button>
  {#if open}
    <ul class="menu" role="listbox" tabindex="-1">
      {#each options as opt, i}
        <li>
          <button
            type="button"
            class="opt mono"
            class:selected={opt === value}
            class:hl={i === highlight}
            role="option"
            aria-selected={opt === value}
            onmouseenter={() => highlight = i}
            onmousedown={(e) => { e.preventDefault(); pick(opt); }}
          >{opt}</button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .sel { position: relative; width: 100%; }

  .trigger {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    background: transparent;
    border: 0;
    border-bottom: 1px solid var(--border);
    color: var(--text);
    font-family: var(--mono);
    font-size: 11px;
    padding: 3px 2px;
    cursor: pointer;
    outline: none;
    border-radius: 0;
    transition: border-color 140ms ease;
    text-align: left;
  }
  .trigger:hover:not(:disabled),
  .trigger:focus-visible,
  .trigger.open { border-bottom-color: var(--atomizer); }
  .trigger:disabled { opacity: 0.6; cursor: default; }

  .val { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .val.placeholder { color: var(--text3); }
  .caret { color: var(--text3); font-size: 10px; line-height: 1; }
  .trigger.open .caret { color: var(--atomizer); }

  .menu {
    position: absolute;
    top: calc(100% + 2px);
    left: 0;
    right: 0;
    z-index: 40;
    list-style: none;
    margin: 0; padding: 4px;
    background: var(--surface2);
    border: 1px solid var(--border2);
    border-radius: 3px;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.6);
    max-height: 220px;
    overflow-y: auto;
  }
  .opt {
    display: block;
    width: 100%;
    text-align: left;
    background: transparent;
    border: 0;
    color: var(--text2);
    font-family: var(--mono);
    font-size: 11px;
    padding: 5px 8px;
    cursor: pointer;
    border-radius: 2px;
    transition: background 80ms ease, color 80ms ease;
  }
  .opt.hl { background: color-mix(in oklab, var(--atomizer) 22%, var(--surface3)); color: var(--text); }
  .opt.selected { color: var(--atomizer); }
  .opt.selected.hl { color: var(--text); }
</style>
