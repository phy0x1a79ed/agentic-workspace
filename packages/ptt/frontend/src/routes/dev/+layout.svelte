<script lang="ts">
  import { base } from '$app/paths';
  let { children } = $props();
</script>

<div class="dev-shell">
  <header class="dev-bar">
    <a class="brand mono" href="{base}/dev">[dev]</a>
    <span class="dim mono">components</span>
    <span class="spacer"></span>
    <a class="back mono" href="{base}/">← app</a>
  </header>
  <main class="dev-content">
    {@render children()}
  </main>
</div>

<style>
  .dev-shell {
    display: grid;
    grid-template-rows: 36px 1fr;
    height: 100dvh;
    background: var(--bg);
    color: var(--text);
  }
  .dev-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 14px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    font-size: 11px;
    letter-spacing: 1px;
  }
  .brand   { color: var(--atomizer); text-decoration: none; }
  .back    { color: var(--text2); text-decoration: none; margin-left: auto; }
  .back:hover, .brand:hover { color: var(--text); }
  .dim     { color: var(--text3); }
  .mono    { font-family: var(--mono); }
  .spacer  { flex: 1; }

  .dev-content {
    overflow: auto;
    padding: 20px;
    min-height: 0;
  }
</style>
