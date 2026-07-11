<script lang="ts">
  /**
   * Config overlay: toggle column visibility and reorder columns. Reads the
   * current order/hidden set, lets the user check/uncheck and move rows up/down,
   * and persists to the notifications fleet contract (which also surfaces the
   * same settings as a card in /ui/settings).
   */
  import { untrack } from 'svelte';
  import { COLUMN_DEFS, ALL_COLUMNS } from './lib/columns';
  import { saveColumns } from './lib/api';

  interface Props {
    order: string[];
    hidden: string[];
    onClose: () => void;
    onSaved: (order: string[], hidden: string[]) => void;
  }
  let { order, hidden, onClose, onSaved }: Props = $props();

  // Working copy: the full ordered key list (any known column missing from the
  // saved order is appended, so newly-added columns still appear to toggle).
  // A one-time snapshot of the incoming props (untrack makes that explicit).
  let rows = $state<string[]>(untrack(() =>
    [...order.filter((k) => COLUMN_DEFS[k]),
     ...ALL_COLUMNS.filter((k) => !order.includes(k))]));
  let hide = $state<Set<string>>(untrack(() => new Set(hidden)));
  let busy = $state(false);

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
      await saveColumns(rows, [...hide]);
      onSaved(rows, [...hide]);
    } finally {
      busy = false;
    }
  }
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape') onClose(); }} />
<div class="ov-scrim" onclick={onClose} role="presentation">
  <div class="ov-sheet" onclick={(e) => e.stopPropagation()} role="dialog"
       aria-modal="true" tabindex="-1" aria-label="columns">
    <header class="ov-head">
      <h2>Columns</h2>
      <button class="ov-x" onclick={onClose} aria-label="close">✕</button>
    </header>

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

    <div class="ov-actions">
      <button class="btn-ghost" onclick={onClose}>cancel</button>
      <button class="btn-go" onclick={save} disabled={busy}>{busy ? 'saving…' : 'save'}</button>
    </div>
  </div>
</div>

<style>
  .ov-scrim {
    position: fixed; inset: 0; z-index: 60;
    background: rgba(0, 0, 0, 0.55);
    display: flex; align-items: flex-end; justify-content: center;
  }
  .ov-sheet {
    width: 100%; max-width: 480px; max-height: 92vh; overflow-y: auto;
    background: var(--surface, #141414);
    border: 1px solid var(--border, #2a2a2a);
    border-radius: 16px 16px 0 0;
    padding: 16px 16px calc(16px + env(safe-area-inset-bottom, 0px));
    display: flex; flex-direction: column; gap: 12px;
  }
  @media (min-width: 560px) {
    .ov-scrim { align-items: center; }
    .ov-sheet { border-radius: 16px; }
  }
  .ov-head { display: flex; align-items: center; justify-content: space-between; }
  .ov-head h2 { font-size: 1rem; margin: 0; }
  .ov-x { background: none; border: none; color: var(--text3, #888); font-size: 1rem; cursor: pointer; }
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
  .ov-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px; }
  .btn-ghost, .btn-go {
    padding: 10px 18px; border-radius: 22px; cursor: pointer; font-size: 0.9rem;
    border: 1px solid var(--border, #333);
  }
  .btn-ghost { background: none; color: var(--text2, #aaa); }
  .btn-go { background: var(--accent, #4b8bff); border-color: transparent; color: #fff; }
  .btn-go:disabled { opacity: 0.5; }
</style>
