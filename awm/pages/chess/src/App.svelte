<script lang="ts">
  import { go, readView, type View } from './lib/api';
  import Lobby from './lib/Lobby.svelte';
  import Live from './lib/Live.svelte';
  import Replay from './lib/Replay.svelte';

  let view = $state<View>(readView());

  $effect(() => {
    const onPop = () => (view = readView());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  });

  function home(e: MouseEvent) {
    e.preventDefault();
    go();
  }
</script>

<div class="page">
  <header class="topbar">
    <h1><a href="?" onclick={home}>♞ chess</a></h1>
  </header>

  {#if view.screen === 'live'}
    {#key view.sid}
      <Live sid={view.sid} />
    {/key}
  {:else if view.screen === 'replay'}
    {#key `${view.save}|${view.session}|${view.game}`}
      <Replay save={view.save} session={view.session} game={view.game} />
    {/key}
  {:else}
    <Lobby />
  {/if}
</div>
