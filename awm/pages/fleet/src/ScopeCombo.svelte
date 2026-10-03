<script lang="ts">
  /**
   * A typable dropdown for the spawn cwd: free-text (any path) with a ▾ button
   * that reveals path suggestions (recent roster cwds + scope worktrees). The
   * explicit button matters on mobile, where native <datalist> is unreliable.
   */
  interface Props {
    value: string;
    suggestions: string[];
    placeholder?: string;
  }
  let { value = $bindable(), suggestions, placeholder = '/path/to/worktree' }: Props =
    $props();

  let open = $state(false);
  let box: HTMLDivElement | undefined = $state();

  const filtered = $derived(
    value.trim()
      ? suggestions.filter((s) => s.toLowerCase().includes(value.toLowerCase().trim()))
      : suggestions,
  );

  function pick(s: string): void {
    value = s;
    open = false;
  }

  function onDocClick(e: MouseEvent): void {
    if (box && !box.contains(e.target as Node)) open = false;
  }
  $effect(() => {
    if (open) {
      document.addEventListener('click', onDocClick, true);
      return () => document.removeEventListener('click', onDocClick, true);
    }
  });
</script>

<div class="combo" bind:this={box}>
  <div class="combo-row">
    <input
      class="combo-input"
      bind:value
      {placeholder}
      onfocus={() => (open = true)}
    />
    <button
      class="combo-toggle"
      type="button"
      aria-label="show suggestions"
      onclick={() => (open = !open)}
    >▾</button>
  </div>
  {#if open && filtered.length > 0}
    <ul class="combo-list">
      {#each filtered.slice(0, 30) as s (s)}
        <li>
          <button type="button" class="combo-item" onclick={() => pick(s)}>{s}</button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .combo { position: relative; }
  .combo-row { display: flex; gap: 6px; }
  .combo-input {
    flex: 1 1 auto; min-width: 0;
    padding: 10px 12px;
    background: var(--surface, #0d0d0d);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 8px);
    color: var(--text, #eee);
    font-size: 16px;
  }
  .combo-toggle {
    flex: 0 0 auto; width: 40px;
    background: var(--surface2, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 8px);
    color: var(--text2, #aaa);
    cursor: pointer;
  }
  .combo-list {
    position: absolute;
    z-index: 5;
    top: calc(100% + 4px);
    left: 0; right: 0;
    max-height: 240px;
    overflow-y: auto;
    margin: 0; padding: 4px;
    list-style: none;
    background: var(--surface2, #191919);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 8px);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  }
  .combo-item {
    display: block; width: 100%;
    padding: 8px 10px;
    text-align: left;
    background: none; border: none;
    color: var(--text, #ddd);
    font-family: var(--mono, monospace);
    font-size: 12px;
    cursor: pointer;
    border-radius: 6px;
    white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .combo-item:hover { background: var(--surface3, #262626); }
</style>
