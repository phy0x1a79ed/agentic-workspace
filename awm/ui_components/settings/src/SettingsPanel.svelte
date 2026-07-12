<script lang="ts">
  /**
   * The settings surface: one card per published config contract.
   *
   * On mount it fetches the gateway aggregator (`/config/contracts`), which folds
   * every opted-in service's contract (+ the gateway global slot) with its live
   * values. Three states:
   *   loading — request in flight.
   *   down    — the aggregator call failed (hub unreachable); a disabled note.
   *   loaded  — a ContractCard per contract (each owns its own save/reset).
   *
   * Lifted out of the dashboard so a dedicated /ui/settings/ page and the
   * dashboard Settings tab render the exact same component.
   */
  import { fetchConfigContracts, type ConfigContract } from '@awm/client';
  import ContractCard from './ContractCard.svelte';

  type PanelState = 'loading' | 'loaded' | 'down';
  let panelState = $state<PanelState>('loading');
  let contracts = $state<ConfigContract[]>([]);

  $effect(() => {
    fetchConfigContracts()
      .then((res) => {
        contracts = res.contracts ?? [];
        panelState = 'loaded';
      })
      .catch(() => {
        panelState = 'down';
      });
  });
</script>

<div class="settings">
  {#if panelState === 'loading'}
    <p class="hint mono">loading settings…</p>
  {:else if panelState === 'down'}
    <p class="hint mono">settings unavailable — the gateway is unreachable</p>
  {:else if contracts.length === 0}
    <p class="hint mono">no services have published a config contract yet</p>
  {:else}
    <div class="cards">
      {#each contracts as contract (contract.key)}
        <ContractCard {contract} />
      {/each}
    </div>
  {/if}
</div>

<style>
  .settings { display: flex; flex-direction: column; gap: var(--space-3); }
  .cards { display: flex; flex-direction: column; gap: var(--space-3); }
  .hint { color: var(--text3); font-size: 12px; padding: var(--space-3) 0; }
  .mono { font-family: var(--mono); }
</style>
