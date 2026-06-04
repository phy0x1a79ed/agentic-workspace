<script lang="ts">
  import { base } from '$app/paths';
  import { fixtureEntries } from '$lib/dev/fixtures';
</script>

<section class="index">
  <h1 class="title mono">components</h1>
  {#if fixtureEntries.length === 0}
    <p class="empty mono">
      no fixtures found.
      drop a <code>*.fixtures.ts</code> next to a component under
      <code>packages/ptt/components/</code>.
    </p>
  {:else}
    <ul class="list">
      {#each fixtureEntries as entry (entry.slug)}
        {@const variantCount = Object.keys(entry.module.default ?? {}).length}
        <li>
          <a class="row mono" href="{base}/dev/components/{entry.slug}">
            <span class="slug">{entry.slug}</span>
            <span class="src dim">{entry.name}.svelte</span>
            <span class="count dim">{variantCount} variant{variantCount === 1 ? '' : 's'}</span>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .index { max-width: 720px; }
  .title {
    font-size: 13px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--text2);
    margin: 0 0 16px;
  }
  .empty { color: var(--text3); font-size: 12px; }
  .list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 2px; }
  .row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    align-items: baseline;
    gap: 16px;
    padding: 8px 10px;
    border: 1px solid transparent;
    border-radius: 3px;
    color: var(--text);
    text-decoration: none;
    font-size: 12px;
    transition: background 120ms ease, border-color 120ms ease;
  }
  .row:hover {
    background: var(--surface2);
    border-color: var(--border);
  }
  .slug  { color: var(--text); }
  .src   { font-size: 10px; }
  .count { font-size: 10px; }
  .dim   { color: var(--text3); }
  .mono  { font-family: var(--mono); }
  code   { font-family: var(--mono); color: var(--text2); }
</style>
