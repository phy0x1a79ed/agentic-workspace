<script lang="ts">
  import '../app.css';
  import Header from '$lib/components/Header.svelte';
  import BottomNav from '$lib/components/BottomNav.svelte';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { api, coreStatus, ApiError } from '$lib/api/client';
  import { ui } from '$lib/state/ui.svelte';

  let { children } = $props();

  // Stale hash bookmarks (#/focus etc.) → history routes.
  onMount(() => {
    const h = location.hash;
    if (h && h.startsWith('#/')) {
      const target = h.slice(1);
      goto(`${base}${target}`, { replaceState: true });
    }
    pollLeader();
    const t = setInterval(pollLeader, 5000);
    return () => clearInterval(t);
  });

  async function pollLeader() {
    try {
      const r = await coreStatus();
      if (r.leadership_state === 'ACTIVE') {
        ui.leaderBadge = 'ACTIVE';
        ui.leaderActive = true;
      } else {
        ui.leaderBadge = `STANDBY → ${r.current_leader || '?'}`;
        ui.leaderActive = false;
      }
      ui.wsKind = 'on';
    } catch (e) {
      ui.wsKind = e instanceof ApiError && e.status === 401 ? 'off' : 'err';
    }
  }
</script>

<div class="shell">
  <Header />
  <main class="content">
    {@render children()}
  </main>
  <BottomNav />
</div>

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
