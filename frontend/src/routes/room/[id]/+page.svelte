<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { page } from '$app/stores';

  // The legacy SPA had a separate "single room" tab. In the new app it's
  // /focus/<id> — same components, same WS. Redirect to keep deep links
  // (and any external integrations posting room links) working.
  onMount(() => {
    const id = $page.params.id;
    goto(`${base}/focus/${encodeURIComponent(id)}`, { replaceState: true });
  });
</script>

<div class="empty-state">
  <div class="empty-icon">◇</div>
  <div class="empty-text">opening room…</div>
</div>
