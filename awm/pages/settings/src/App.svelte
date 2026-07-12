<script lang="ts">
  /**
   * Settings page — a thin shell around the shared @awm/settings SettingsPanel.
   * One kind=page bundle served at /ui/settings/. The panel does all the work
   * (fetch the gateway aggregator, render a card per published config contract);
   * this page only supplies the chrome so the same component also drops into the
   * dashboard Settings tab.
   */
  import { SettingsPanel } from '@awm/settings';
</script>

<main class="page">
  <header class="top">
    <h1 class="mono">settings</h1>
  </header>
  <section class="wrap">
    <SettingsPanel />
  </section>
</main>

<style>
  .page {
    min-height: 100vh;
    color: var(--text);
    background: var(--bg);
    font-family: var(--sans, system-ui, sans-serif);
  }
  .top {
    padding: var(--space-3) var(--space-4);
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  h1 {
    margin: 0;
    font-size: 13px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .wrap {
    max-width: 720px;
    margin: 0 auto;
    padding: var(--space-4);
  }
  .mono { font-family: var(--mono); }
</style>
