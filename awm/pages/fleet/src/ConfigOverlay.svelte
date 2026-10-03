<script lang="ts">
  /**
   * Settings page: desktop-notification preferences + roster column visibility
   * and order. Renders in the shared full-screen FleetSheet (like the terminal),
   * not a modal. Persists to the notifications fleet contract (which also
   * surfaces the same settings as a card in /ui/settings).
   */
  import { untrack } from 'svelte';
  import { COLUMN_DEFS, ALL_COLUMNS } from './lib/columns';
  import { saveColumns, saveNotificationsEnabled } from './lib/api';
  import FleetSheet from './FleetSheet.svelte';

  interface Props {
    order: string[];
    hidden: string[];
    notificationsEnabled: boolean;
    notifPerm: NotificationPermission;
    onRequestPermission: () => void;
    onClose: () => void;
    onSaved: (order: string[], hidden: string[], notificationsEnabled: boolean) => void;
  }
  let {
    order, hidden, notificationsEnabled, notifPerm,
    onRequestPermission, onClose, onSaved,
  }: Props = $props();

  // Working copy: the full ordered key list (any known column missing from the
  // saved order is appended, so newly-added columns still appear to toggle).
  // A one-time snapshot of the incoming props (untrack makes that explicit).
  let rows = $state<string[]>(untrack(() =>
    [...order.filter((k) => COLUMN_DEFS[k]),
     ...ALL_COLUMNS.filter((k) => !order.includes(k))]));
  let hide = $state<Set<string>>(untrack(() => new Set(hidden)));
  let notifOn = $state<boolean>(untrack(() => notificationsEnabled));
  let busy = $state(false);

  const permGranted = $derived(notifPerm === 'granted');

  function toggle(k: string): void {
    const next = new Set(hide);
    if (next.has(k)) next.delete(k); else next.add(k);
    hide = next;
  }
  function move(i: number, d: -1 | 1): void {
    const j = i + d;
    if (j < 0 || j >= rows.length) return;
    const next = [...rows];
    [next[i], next[j]] = [next[j], next[i]];
    rows = next;
  }
  async function save(): Promise<void> {
    busy = true;
    try {
      await Promise.all([
        saveColumns(rows, [...hide]),
        saveNotificationsEnabled(notifOn),
      ]);
      onSaved(rows, [...hide], notifOn);
    } finally {
      busy = false;
    }
  }
</script>

<FleetSheet title="Settings" onClose={onClose}>
  <section class="grp">
    <h3 class="grp-h">Desktop notifications</h3>
    <label class="switch">
      <input type="checkbox" bind:checked={notifOn} />
      <span class="switch-l">
        Push needs-you &amp; error pings
        <span class="switch-sub">alerts land even when this tab is backgrounded</span>
      </span>
    </label>
    {#if notifOn && !permGranted}
      <div class="perm">
        {#if notifPerm === 'denied'}
          <span class="perm-note">Browser notifications are blocked — allow them in
            your browser's site settings for pushes to appear.</span>
        {:else}
          <button class="perm-btn" onclick={onRequestPermission}>
            Grant browser permission
          </button>
        {/if}
      </div>
    {/if}
  </section>

  <section class="grp">
    <h3 class="grp-h">Columns</h3>
    <ul class="cols">
      {#each rows as k, i (k)}
        <li class="col-row" class:off={hide.has(k)}>
          <label class="col-vis">
            <input type="checkbox" checked={!hide.has(k)} onchange={() => toggle(k)} />
            <span class="col-name">{COLUMN_DEFS[k]?.label || k}</span>
            <span class="col-key">{k}</span>
          </label>
          <div class="col-move">
            <button onclick={() => move(i, -1)} disabled={i === 0} aria-label="up">↑</button>
            <button onclick={() => move(i, 1)} disabled={i === rows.length - 1} aria-label="down">↓</button>
          </div>
        </li>
      {/each}
    </ul>
  </section>

  {#snippet footer()}
    <button class="btn-ghost" onclick={onClose}>cancel</button>
    <button class="btn-go" onclick={save} disabled={busy}>{busy ? 'saving…' : 'save'}</button>
  {/snippet}
</FleetSheet>

<style>
  .grp { display: flex; flex-direction: column; gap: 8px; }
  .grp-h {
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--text3, #888); margin: 0 0 2px;
  }

  .switch {
    display: flex; align-items: flex-start; gap: 10px;
    padding: 10px 12px;
    background: var(--surface2, #191919);
    border: 1px solid var(--border, #2a2a2a);
    border-radius: var(--radius-md, 8px);
    cursor: pointer;
  }
  .switch input { margin-top: 2px; width: 18px; height: 18px; accent-color: var(--accent, #4b8bff); }
  .switch-l { display: flex; flex-direction: column; gap: 2px; font-size: 0.9rem; }
  .switch-sub { font-size: 0.72rem; color: var(--text3, #888); }
  .perm { padding: 2px 2px 0; }
  .perm-note { font-size: 0.78rem; color: var(--warn, #e5c07b); }
  .perm-btn {
    padding: 8px 14px; border-radius: 18px; cursor: pointer; font-size: 0.82rem;
    background: none; border: 1px solid var(--accent, #4b8bff); color: var(--accent, #4b8bff);
  }

  .cols { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .col-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 10px;
    background: var(--surface2, #191919);
    border: 1px solid var(--border, #2a2a2a);
    border-radius: var(--radius-md, 8px);
  }
  .col-row.off { opacity: 0.5; }
  .col-vis { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .col-name { font-size: 0.9rem; }
  .col-key { font-size: 0.68rem; color: var(--text3, #888); font-family: var(--mono, monospace); }
  .col-move { display: flex; gap: 4px; }
  .col-move button {
    width: 30px; height: 30px;
    background: var(--surface3, #242424);
    border: 1px solid var(--border, #333);
    border-radius: 6px; color: var(--text2, #bbb); cursor: pointer;
  }
  .col-move button:disabled { opacity: 0.35; }
  .btn-ghost, .btn-go {
    padding: 10px 18px; border-radius: 22px; cursor: pointer; font-size: 0.9rem;
    border: 1px solid var(--border, #333);
  }
  .btn-ghost { background: none; color: var(--text2, #aaa); }
  .btn-go { background: var(--accent, #4b8bff); border-color: transparent; color: #fff; }
  .btn-go:disabled { opacity: 0.5; }
</style>
