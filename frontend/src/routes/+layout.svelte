<script lang="ts">
  import '../app.css';
  import Header from '$lib/components/Header.svelte';
  import BottomNav from '$lib/components/BottomNav.svelte';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { page } from '$app/stores';
  import { ensureVagrantSession } from '$lib/api/client';
  import { ui } from '$lib/state/ui.svelte';

  let { children } = $props();

  // `/dev/*` is the isolated component dev surface — no backend, no app shell.
  const isDev = $derived($page.url.pathname.startsWith(`${base}/dev`) || $page.url.pathname.startsWith('/dev'));

  onMount(() => {
    if (isDev) return;
    const h = location.hash;
    if (h && h.startsWith('#/')) {
      const target = h.slice(1);
      goto(`${base}${target}`, { replaceState: true });
    }
    ui.startPing();
    bootstrapManager();
    return () => {
      ui.stopPing();
    };
  });

  async function bootstrapManager() {
    try {
      const r = await ensureVagrantSession();
      ui.managerScope = r.scope_identifier;
    } catch {
      // Non-fatal — user can still browse other rooms.
    }
  }
</script>

{#if isDev}
  {@render children()}
{:else}
  <div class="shell">
    <Header />
    <main class="content">
      {@render children()}
    </main>
    <BottomNav />
  </div>
{/if}

<style>
  .shell {
    display: grid;
    grid-template-rows: 44px 1fr;
    height: 100dvh;
    background: var(--bg);
  }
  .content { overflow: hidden; min-height: 0; display: flex; flex-direction: column; }

  @media (max-width: 1024px) {
    .shell { grid-template-rows: 44px 1fr 52px; }
  }
</style>
