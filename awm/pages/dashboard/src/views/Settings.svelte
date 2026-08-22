<script lang="ts">
  /**
   * Dashboard Settings tab — now just the shared @awm/settings SettingsPanel, so
   * the dashboard and the standalone /ui/settings/ page render one identical
   * implementation. The former hand-rolled (non-wired) stub for global awm
   * config is gone; the panel renders every published config contract live.
   */
  import { SettingsPanel } from '@awm/settings';
</script>

<div class="settings">
  <SettingsPanel />
</div>

<style>
  .settings { padding: var(--space-5); max-width: 720px; }
</style>
