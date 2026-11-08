<script lang="ts">
  // The dashboard shell. Its one section is the settings panel. The overview
  // and projects tabs went with their mock task data, which no service served.
  import Settings from './views/Settings.svelte';
</script>

<div class="shell">
  <header class="strip">
    <span class="brand">awm<span class="dim">/dashboard</span></span>
  </header>
  <main class="view">
    <Settings />
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

  .view { flex: 1; min-height: 0; }
</style>
