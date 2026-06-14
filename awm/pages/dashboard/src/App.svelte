<script lang="ts">
  // ─────────────────────────────────────────────────────────────────────
  // Dashboard shell — mobile-first, tabbed, LAYOUT ONLY.
  //
  // Four sections (Overview / Projects / Agents / Settings) swapped by a
  // hash-driven tab bar. The bar sits at the bottom on phones (thumb reach)
  // and promotes to the top on wide screens. No router dependency; one
  // kind=page bundle served at /ui/dashboard/.
  //
  // Everything rendered is placeholder/skeleton or static layout — no live
  // data, no functional components. Real components get merged in from other
  // scopes later (see src/lib/tasks/CONTRACT.md and .awm/context.md).
  // ─────────────────────────────────────────────────────────────────────
  import { onMount } from 'svelte';
  import Overview from './views/Overview.svelte';
  import Projects from './views/Projects.svelte';
  import Agents from './views/Agents.svelte';
  import Settings from './views/Settings.svelte';

  type View = 'overview' | 'projects' | 'agents' | 'settings';
  const TABS: { id: View; label: string; glyph: string }[] = [
    { id: 'overview', label: 'overview', glyph: '▤' },
    { id: 'projects', label: 'projects', glyph: '▦' },
    { id: 'agents', label: 'agents', glyph: '@' },
    { id: 'settings', label: 'settings', glyph: '=' }
  ];

  let view = $state<View>('overview');
  let requestedProject = $state('');

  function parseHash(): View {
    const h = window.location.hash.replace(/^#\/?/, '').split('/')[0];
    return (TABS.find((t) => t.id === h)?.id ?? 'overview') as View;
  }
  function go(v: View) {
    if (window.location.hash !== `#/${v}`) window.location.hash = `#/${v}`;
    view = v;
  }
  function openProject(name: string) {
    requestedProject = name;
    go('projects');
  }

  onMount(() => {
    view = parseHash();
    const onHash = () => (view = parseHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  });
</script>

<div class="shell">
  <header class="strip">
    <span class="brand">awm<span class="dim">/dashboard</span></span>
    <span class="health"><span class="hdot"></span>hub —</span>
    <span class="scopes">active scopes —</span>
  </header>

  <nav class="tabs">
    {#each TABS as t (t.id)}
      <button class="tab" class:active={view === t.id} onclick={() => go(t.id)}>
        <span class="glyph">[{t.glyph}]</span>
        <span class="tlabel">{t.label}</span>
      </button>
    {/each}
  </nav>

  <main class="view">
    {#if view === 'overview'}
      <Overview onopenproject={openProject} />
    {:else if view === 'projects'}
      <Projects {requestedProject} />
    {:else if view === 'agents'}
      <Agents />
    {:else if view === 'settings'}
      <Settings />
    {/if}
  </main>
</div>

<style>
  .shell {
    display: flex;
    flex-direction: column;
    min-height: 100vh;
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
  }

  /* status strip */
  .strip {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    padding: var(--space-3) var(--space-4);
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    font-family: var(--mono);
    font-size: 11px;
  }
  .brand { font-weight: 600; letter-spacing: 0.04em; }
  .brand .dim { color: var(--text3); }
  .health, .scopes { color: var(--text2); display: flex; align-items: center; gap: var(--space-2); }
  .scopes { margin-left: auto; }
  .hdot { width: 7px; height: 7px; border-radius: 50%; background: var(--text3); }

  /* view region — leaves room for the fixed bottom tab bar on mobile */
  .view { flex: 1; min-height: 0; padding-bottom: 64px; }

  /* tab bar — bottom on mobile */
  .tabs {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    z-index: 10;
    display: flex;
    background: var(--surface);
    border-top: 1px solid var(--border);
  }
  .tab {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    padding: var(--space-3) 0 calc(var(--space-3) + env(safe-area-inset-bottom, 0px));
    background: none;
    border: none;
    color: var(--text3);
    font: inherit;
    cursor: pointer;
  }
  .tab .glyph { font-family: var(--mono); font-size: 12px; }
  .tab .tlabel { font-size: 10px; letter-spacing: 0.04em; }
  .tab.active { color: var(--atomizer); }

  /* desktop: tab bar moves to the top row, view region capped */
  @media (min-width: 720px) {
    .tabs {
      position: static;
      border-top: none;
      border-bottom: 1px solid var(--border);
      padding: 0 var(--space-3);
    }
    .tab {
      flex: none;
      flex-direction: row;
      gap: var(--space-2);
      padding: var(--space-3) var(--space-4);
      border-bottom: 2px solid transparent;
    }
    .tab.active { border-bottom-color: var(--atomizer); }
    .tab .tlabel { font-size: 12px; }
    .view { padding-bottom: 0; }
  }
</style>
