<script lang="ts">
  import { Button, Input, PanelLabel, Select } from '@awm/primitives';

  // Laid out, not wired. The config store (config_service.get/set_config) has
  // no HTTP route yet; GET/PUT /config is in tasks/CONTRACT.md. Values here are
  // placeholder defaults so the form has shape.
  let agentCli = $state('claude');
  let vagrantRepo = $state('');
</script>

<div class="settings">
  <PanelLabel tone="dim">settings · awm config</PanelLabel>
  <p class="hint">key/value config — not wired yet (awaits GET/PUT /config)</p>

  <div class="rows">
    <div class="row">
      <label for="cli">default agent CLI</label>
      <div class="ctl">
        <Select value={agentCli} options={['claude', 'opencode']} onchange={(v: string) => (agentCli = v)} />
      </div>
    </div>

    <div class="row">
      <label for="vrepo">vagrant scopes repo URL</label>
      <div class="ctl">
        <Input bind:value={vagrantRepo} placeholder="git@github.com:…" />
      </div>
    </div>
  </div>

  <div class="actions">
    <Button kind="primary" size="sm">Save</Button>
  </div>
</div>

<style>
  .settings { padding: var(--space-5); display: flex; flex-direction: column; gap: var(--space-3); max-width: 560px; }
  .hint { font-size: 10px; color: var(--text3); font-family: var(--mono); }
  .rows { display: flex; flex-direction: column; gap: var(--space-4); margin-top: var(--space-2); }
  .row { display: flex; flex-direction: column; gap: var(--space-2); }
  label { font-size: 11px; color: var(--text2); font-family: var(--mono); }
  .ctl { max-width: 360px; }
  .actions { margin-top: var(--space-4); }

  @media (min-width: 720px) {
    .row { flex-direction: row; align-items: center; }
    label { width: 200px; flex: none; }
  }
</style>
