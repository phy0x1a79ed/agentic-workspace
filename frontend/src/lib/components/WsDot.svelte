<script lang="ts">
  import { ui } from '$lib/state/ui.svelte';
</script>

<span class="ws-dot" class:off={ui.wsKind === 'off'} class:err={ui.wsKind === 'err'}></span>
