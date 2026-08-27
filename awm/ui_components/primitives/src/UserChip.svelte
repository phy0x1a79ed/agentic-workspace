<script lang="ts">
  /**
   * Who is signed in, where else they can go, and the way out.
   *
   * Asks the edge (`/__auth/whoami`) once on mount. When there is no edge —
   * the page was opened straight off the loopback gateway — the chip stays
   * hidden rather than showing a guessed name.
   */
  import { onMount } from 'svelte';
  import { whoami, logout } from '@awm/client';

  interface Props {
    links?: { label: string; href: string }[];
  }
  let { links = [] }: Props = $props();

  let user = $state<string | null>(null);

  onMount(async () => {
    try {
      user = (await whoami()).user;
    } catch {
      user = null;
    }
  });
</script>

{#if user}
  <div class="userchip" data-awm-component="UserChip">
    <span class="who" title="Signed in as {user}">{user}</span>
    {#each links as l (l.href)}
      <a class="lnk" href={l.href}>{l.label}</a>
    {/each}
    <button class="out" type="button" onclick={() => void logout()} title="Sign out">Sign out</button>
  </div>
{/if}

<style>
  .userchip {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8rem;
    padding: 0.15rem 0.5rem;
    border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
    border-radius: 999px;
    white-space: nowrap;
  }
  .who { font-weight: 600; }
  .lnk { color: inherit; opacity: 0.8; text-decoration: none; }
  .lnk:hover { opacity: 1; text-decoration: underline; }
  .out {
    font: inherit;
    color: inherit;
    background: none;
    border: 0;
    padding: 0;
    cursor: pointer;
    opacity: 0.8;
  }
  .out:hover { opacity: 1; text-decoration: underline; }
</style>
