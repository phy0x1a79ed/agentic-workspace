<script lang="ts">
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { findBySlug } from '$lib/dev/fixtures';

  const slug = $derived($page.params.name ?? '');
  const entry = $derived(slug ? findBySlug(slug) : undefined);

  const variants = $derived(entry ? Object.keys(entry.module.default ?? {}) : []);
  const queryVariant = $derived($page.url.searchParams.get('v'));
  const variant = $derived(queryVariant && variants.includes(queryVariant) ? queryVariant : variants[0] ?? null);
  const props = $derived(entry && variant ? entry.module.default[variant] : null);

  function pickVariant(v: string) {
    const url = new URL($page.url);
    url.searchParams.set('v', v);
    goto(url.pathname + url.search, { replaceState: true, noScroll: true });
  }
</script>

<section class="page">
  <header class="head mono">
    <a class="back" href="{base}/dev">← components</a>
    <span class="title">{slug}</span>
    {#if entry}
      <span class="src dim">{entry.name}.svelte</span>
    {/if}
    <span class="spacer"></span>
    {#if variants.length > 1}
      <span class="variants">
        {#each variants as v (v)}
          <button
            class="vbtn"
            class:active={v === variant}
            type="button"
            onclick={() => pickVariant(v)}
          >{v}</button>
        {/each}
      </span>
    {/if}
  </header>

  <div class="stage">
    {#if !entry}
      <p class="warn mono">no fixture found for slug <code>{slug}</code>.</p>
    {:else if !variant}
      <p class="warn mono">{entry.name}.fixtures.ts has no variants.</p>
    {:else}
      {@const Component = entry.module.component as any}
      <Component {...props} />
    {/if}
  </div>
</section>

<style>
  .page { display: flex; flex-direction: column; gap: 16px; height: 100%; }
  .head {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 11px;
    letter-spacing: 1px;
    color: var(--text2);
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }
  .back { color: var(--text3); text-decoration: none; }
  .back:hover { color: var(--text); }
  .title { color: var(--text); }
  .src   { font-size: 10px; }
  .dim   { color: var(--text3); }
  .spacer { flex: 1; }
  .variants { display: inline-flex; gap: 4px; }
  .vbtn {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text2);
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 1px;
    padding: 3px 8px;
    border-radius: 3px;
    cursor: pointer;
  }
  .vbtn:hover { background: var(--surface2); color: var(--text); }
  .vbtn.active {
    border-color: color-mix(in oklab, var(--atomizer) 60%, var(--border));
    color: var(--atomizer);
  }
  .stage {
    flex: 1;
    padding: 20px;
    border: 1px dashed var(--border);
    border-radius: 4px;
    overflow: auto;
    min-height: 0;
  }
  .warn { color: var(--text3); font-size: 12px; }
  .mono { font-family: var(--mono); }
  code  { font-family: var(--mono); color: var(--text2); }
</style>
