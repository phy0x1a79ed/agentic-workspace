<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/stores';

  const items = [
    { id: 'status', label: 'STATUS', href: `${base}/status` },
    { id: 'focus',  label: 'FOCUS',  href: `${base}/focus`  },
    { id: 'rooms',  label: 'ROOMS',  href: `${base}/rooms`  }
  ];

  function activeId(url: URL): string {
    const p = url.pathname.replace(base, '') || '/';
    if (p.startsWith('/focus')) return 'focus';
    if (p.startsWith('/rooms')) return 'rooms';
    if (p.startsWith('/room'))  return 'rooms';
    return 'status';
  }
</script>

<nav class="bottom-nav" aria-label="Sections">
  {#each items as it}
    <a class="item" class:active={activeId($page.url) === it.id} href={it.href}>
      <span class="label">{it.label}</span>
    </a>
  {/each}
</nav>

<style>
  .bottom-nav {
    display: none;
    grid-template-columns: repeat(3, 1fr);
    background: var(--surface);
    border-top: 1px solid var(--border);
    padding-bottom: env(safe-area-inset-bottom);
  }
  .item {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 14px 4px;
    color: var(--text3);
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    border-top: 2px solid transparent;
    margin-top: -1px;
    transition: color 0.12s, border-color 0.12s;
  }
  .item.active {
    color: var(--text);
    border-top-color: var(--atomizer);
    background: color-mix(in oklab, var(--atomizer) 7%, transparent);
  }

  @media (max-width: 1024px) {
    .bottom-nav { display: grid; }
  }
</style>
