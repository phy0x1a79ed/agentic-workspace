<script lang="ts">
  export type StatRow = {
    label: string;
    value: string | number | null | undefined;
    mono?: boolean;
    dim?: boolean;
  };

  interface Props {
    rows: StatRow[];
    compact?: boolean;
    labelWidth?: string;
  }
  let { rows, compact = false, labelWidth = '88px' }: Props = $props();
</script>

<dl class="stats" class:compact style="--lbl-w: {labelWidth}">
  {#each rows as r}
    <div class="row">
      <dt class="k">{r.label}</dt>
      <dd class="v" class:mono={r.mono} class:dim={r.dim || r.value == null || r.value === ''}>
        {r.value == null || r.value === '' ? '—' : r.value}
      </dd>
    </div>
  {/each}
</dl>

<style>
  .stats {
    display: grid;
    grid-template-columns: 1fr;
    gap: var(--space-0);
    margin: 0;
    padding: 0;
  }
  .stats.compact { font-size: 10px; }
  .row {
    display: grid;
    grid-template-columns: var(--lbl-w) 1fr;
    gap: var(--space-3);
    align-items: baseline;
    font-family: var(--mono);
    font-size: 10px;
    padding: var(--space-0) 0;
  }
  .k {
    margin: 0;
    text-align: right;
    color: var(--text3);
    letter-spacing: 0.6px;
    text-transform: uppercase;
    font-weight: 500;
    font-size: 9px;
  }
  .v {
    margin: 0;
    color: var(--text2);
    word-break: break-word;
    white-space: pre-wrap;
  }
  .v.dim { color: var(--text3); font-style: italic; }
  .v.mono { font-family: var(--mono); }
  @media (max-width: 480px) {
    .row { grid-template-columns: 1fr; gap: 0; }
    .k { text-align: left; }
  }
</style>
