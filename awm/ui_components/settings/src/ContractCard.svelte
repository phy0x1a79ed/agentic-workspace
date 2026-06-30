<script lang="ts">
  /**
   * One config contract, rendered as a collapsible card with a dynamic form +
   * Save/Reset. Owns its own working copy and last-saved baseline, so the
   * dirty/disabled state and the Save round-trip are local to this contract.
   * Three shapes:
   *   unavailable — the owning service is down; disabled note, never a form.
   *   no fields   — an empty schema (e.g. the scaffolded gateway global slot);
   *                 a muted "no settings yet" note.
   *   editable    — the DynamicConfigForm bound to the working values.
   */
  import { untrack } from 'svelte';
  import { Button, CollapsibleSection } from '@awm/primitives';
  import { saveConfigContract, type ConfigContract } from '@awm/client';
  import DynamicConfigForm from './DynamicConfigForm.svelte';

  interface Props {
    contract: ConfigContract;
  }
  let { contract }: Props = $props();

  // Last-saved baseline + the in-progress working copy. Plain snapshots so the
  // dirty check is a structural compare, not reference identity. The card is
  // keyed by contract.key in the parent, so a one-time snapshot of the initial
  // values is intended (untrack makes that explicit, no false reactivity warn).
  const initial = untrack(() => ({ ...(contract.values ?? {}) }));
  let baseline = $state<Record<string, unknown>>({ ...initial });
  let working = $state<Record<string, unknown>>({ ...initial });
  let saving = $state(false);
  let error = $state<string | null>(null);
  let savedFlash = $state(false);
  let open = $state(true);

  const fields = $derived(
    Object.keys(
      (contract.schema?.properties as Record<string, unknown> | undefined) ?? {},
    ),
  );
  const hasFields = $derived(fields.length > 0);
  const dirty = $derived(JSON.stringify(working) !== JSON.stringify(baseline));

  function onchange(next: Record<string, unknown>) {
    working = next;
    savedFlash = false;
  }

  function reset() {
    working = { ...baseline };
    error = null;
    savedFlash = false;
  }

  async function save() {
    saving = true;
    error = null;
    try {
      const saved = await saveConfigContract(contract.key, working);
      baseline = { ...(saved.values ?? {}) };
      working = { ...(saved.values ?? {}) };
      savedFlash = true;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      saving = false;
    }
  }
</script>

<CollapsibleSection label={contract.title} rail="manager" bind:open>
  {#if contract.unavailable}
    <div class="note mono">service unavailable — settings can't be loaded</div>
  {:else if !hasFields}
    <div class="note mono">no settings defined yet</div>
  {:else}
    <div class="body">
      <DynamicConfigForm
        schema={contract.schema}
        value={working}
        {onchange}
        disabled={saving}
      />
      <div class="actions">
        <Button kind="primary" onclick={save} disabled={!dirty || saving}>
          {saving ? 'saving…' : 'save'}
        </Button>
        <Button kind="ghost" onclick={reset} disabled={!dirty || saving}>reset</Button>
        {#if savedFlash}<span class="ok mono">saved</span>{/if}
        {#if error}<span class="err mono">{error}</span>{/if}
      </div>
    </div>
  {/if}
</CollapsibleSection>

<style>
  .body { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-2) 0; }
  .note { color: var(--text3); font-size: 11px; font-style: italic; padding: var(--space-2) 0; }
  .actions { display: flex; align-items: center; gap: var(--space-2); }
  .mono { font-family: var(--mono); }
  .ok { color: var(--ok, #7fd17f); font-size: 11px; }
  .err { color: var(--danger, #f55); font-size: 11px; }
</style>
