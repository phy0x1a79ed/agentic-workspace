<script lang="ts">
  import { base } from '$app/paths';
  import { page } from '$app/stores';
  import LiveIndicator from './LiveIndicator.svelte';
  import { awmAs } from '$lib/api/client';

  const tabs = [
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

<header class="header">
  <span class="logo">AWM</span>

  <nav class="tabs">
    {#each tabs as t}
      <a class="tab" class:active={activeId($page.url) === t.id} href={t.href}>{t.label}</a>
    {/each}
  </nav>

  <span class="spacer"></span>

  <span class="meta">
    <span class="user">{awmAs().replace(/^user:/, '')}</span>
    <LiveIndicator />
  </span>
</header>

<style>
  .header {
    grid-column: 1 / -1;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    padding: 0 18px;
    gap: 14px;
    user-select: none;
  }
  .logo {
    font-family: var(--mono);
    font-size: 18px;
    font-weight: 500;
    letter-spacing: 3px;
    color: var(--text);
  }
  .spacer { flex: 1; }
  .tabs   { display: flex; align-self: stretch; }
  .tab {
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
    padding: 0 16px;
    display: flex;
    align-items: center;
    color: var(--text3);
    transition: color 0.15s, background 0.15s;
  }
  .tab:hover  { color: var(--text2); }
  .tab.active { color: var(--text); border-bottom: 2px solid var(--atomizer); }

  .meta {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text3);
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .meta .user { display: inline-flex; align-items: center; }

  @media (max-width: 1024px) {
    .tabs { display: none; }
  }
  @media (max-width: 720px) {
    .header { padding: 0 12px; gap: 10px; }
    .logo { font-size: 14px; letter-spacing: 2px; }
    .meta { font-size: 9px; gap: 8px; }
  }
</style>
