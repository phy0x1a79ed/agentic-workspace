<script lang="ts">
  /**
   * Per-agent config form (manager-only). Renders mode/effort selects + a
   * model input in a config-file `key: value` grid, with a Restart button
   * that commits the form and a Kill (danger). Restart issues the minimum
   * sequence of slash commands needed to reach the form's values — each
   * sub-command respawns the agent and inherits the rest, so worst case is
   * 3 respawns (model + effort + restart-with-mode).
   *
   * Lives inside the collapsible card body; compact / clear are on the
   * always-visible card header in `AgentList.svelte`.
   */
  import { runAgentSlash, ApiError, type LiveAgent } from '$lib/api/client';
  import Select from './Select.svelte';
  import Button from '$lib/primitives/Button.svelte';
  import PanelLabel from '$lib/primitives/PanelLabel.svelte';

  interface Props {
    roomId: string;
    scope: string;
    live?: LiveAgent | null;
    onresult?: (msg: string) => void;
    /** Fires once Restart has committed (after the final respawn settles). */
    oncommitted?: () => void;
  }
  let { roomId, scope, live = null, onresult, oncommitted }: Props = $props();

  const MODE_CHOICES   = ['default', 'acceptEdits', 'auto', 'bypassPermissions', 'dontAsk', 'plan'] as const;
  const EFFORT_CHOICES = ['low', 'medium', 'high', 'xhigh', 'max'] as const;
  // Canonical model aliases the backend's /model handler accepts. The CLI
  // also accepts a full id (e.g. claude-haiku-4-5-20251001) but those are
  // niche — keep the dropdown to the three names everyone uses.
  const MODEL_CHOICES  = ['opus', 'sonnet', 'haiku'] as const;

  // Local form state — re-seeded from `live` on mount and whenever the
  // backend poll surfaces a new pid/mode/model/effort signature, so the
  // form reflects the agent's actual current settings until user edits.
  let modeVal   = $state<string>('default');
  let modelVal  = $state<string>('');
  let effortVal = $state<string>('medium');
  let lastSeenLive = $state<string>('');

  $effect(() => {
    const sig = serializeLive(live);
    if (sig !== lastSeenLive) {
      lastSeenLive = sig;
      modeVal   = live?.permission_mode ?? 'default';
      modelVal  = live?.model ?? '';
      effortVal = live?.effort ?? 'medium';
    }
  });

  function serializeLive(l: LiveAgent | null | undefined): string {
    if (!l) return '';
    return `${l.permission_mode ?? ''}|${l.model ?? ''}|${l.effort ?? ''}`;
  }

  let busy = $state<boolean>(false);

  async function commit() {
    if (busy) return;
    busy = true;
    try {
      const changes: string[] = [];

      if (modelVal && modelVal !== (live?.model ?? '')) {
        await fire(`/model ${shellQuote(modelVal)}`);
        changes.push(`model=${modelVal}`);
      }
      if (effortVal && effortVal !== (live?.effort ?? '')) {
        await fire(`/effort ${effortVal}`);
        changes.push(`effort=${effortVal}`);
      }

      // Always /restart; pass mode arg if it changed (or if it was never set).
      const modeChanged = modeVal && modeVal !== (live?.permission_mode ?? '');
      const cmd = modeChanged ? `/restart ${modeVal}` : '/restart';
      await fire(cmd);
      if (modeChanged) changes.push(`mode=${modeVal}`);

      const summary = changes.length
        ? `restarted: ${changes.join(' ')}`
        : 'restarted';
      onresult?.(summary);
      oncommitted?.();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      onresult?.(msg);
    } finally {
      busy = false;
    }
  }

  async function fire(cmd: string): Promise<void> {
    const r = await runAgentSlash(roomId, scope, cmd);
    // Surface intermediate errors via onresult but don't break the chain on
    // success — the handler returns its own status string.
    if (!r.handled) onresult?.(`unhandled: ${cmd}`);
  }

  function shellQuote(s: string): string {
    return /[\s"']/.test(s) ? `"${s.replace(/"/g, '\\"')}"` : s;
  }

  async function kill() {
    if (busy) return;
    if (typeof window !== 'undefined' &&
        !window.confirm(`Kill agent ${scope}? Stops the manager process.`)) return;
    busy = true;
    try {
      const r = await runAgentSlash(roomId, scope, '/kill');
      onresult?.(r.result || 'killed');
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      onresult?.(msg);
    } finally {
      busy = false;
    }
  }
</script>

<form class="cfg" onsubmit={(e) => { e.preventDefault(); commit(); }}>
  <div class="row">
    <span class="k"><PanelLabel>mode</PanelLabel></span>
    <Select
      value={modeVal}
      options={MODE_CHOICES}
      disabled={busy}
      onchange={(v) => modeVal = v}
    />
  </div>
  <div class="row">
    <span class="k"><PanelLabel>effort</PanelLabel></span>
    <Select
      value={effortVal}
      options={EFFORT_CHOICES}
      disabled={busy}
      onchange={(v) => effortVal = v}
    />
  </div>
  <div class="row">
    <span class="k"><PanelLabel>model</PanelLabel></span>
    <Select
      value={modelVal}
      options={MODEL_CHOICES}
      disabled={busy}
      placeholder="—"
      onchange={(v) => modelVal = v}
    />
  </div>

  <div class="actions">
    <Button size="sm" kind="ghost" type="submit" disabled={busy}>
      {busy ? '…' : 'restart'}
    </Button>
    <Button size="sm" kind="danger" disabled={busy} onclick={kill}>
      kill
    </Button>
  </div>
</form>

<style>
  .cfg {
    display: grid;
    grid-template-columns: 1fr;
    gap: 4px;
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed color-mix(in oklab, var(--border) 80%, transparent);
  }
  .row {
    display: grid;
    grid-template-columns: 56px 1fr;
    align-items: center;
    gap: 8px;
    min-height: 22px;
  }
  .k { text-align: right; }

  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 6px;
    margin-top: 8px;
  }
</style>
