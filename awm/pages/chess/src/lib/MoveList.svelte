<script lang="ts">
  interface Move {
    ply: number;
    side: 'white' | 'black';
    san: string;
    uci: string;
    think_s?: number | null;
  }
  interface Props {
    moves: Move[];
    rejected?: { ply: number; side: string; move: string; reason: string }[];
    current?: number | null;
    follow?: boolean;
    onselect?: (index: number) => void;
  }
  let { moves, rejected = [], current = null, follow = false, onselect }: Props = $props();

  interface Row {
    num: number;
    white: number | null;
    black: number | null;
    rejected: Props['rejected'] & {};
  }

  const firstSide = $derived(moves[0]?.side ?? rejected[0]?.side ?? 'white');

  const rows = $derived.by(() => {
    const offset = firstSide === 'black' ? 1 : 0;
    const out: Row[] = [];
    const row = (ply: number) => {
      const i = Math.floor((ply + offset) / 2);
      while (out.length <= i) out.push({ num: out.length + 1, white: null, black: null, rejected: [] });
      return out[i];
    };
    moves.forEach((m, i) => { row(m.ply)[m.side] = i; });
    for (const r of rejected) row(r.ply).rejected.push(r);
    return out;
  });

  let el: HTMLDivElement;

  $effect(() => {
    if (follow && el && rows.length) el.scrollTop = el.scrollHeight;
  });

  const think = (m: Move) => (m.think_s != null ? `${m.think_s.toFixed(2)} s` : '');
</script>

<div class="moves" bind:this={el}>
  {#each rows as r (r.num)}
    <span class="num">{r.num}.</span>
    {#each [r.white, r.black] as index, k (k)}
      {#if index === null}
        <span class="faint">{k === 0 && r.black !== null ? '…' : ''}</span>
      {:else}
        <span>
          <button class:current={current === index} title={think(moves[index])}
                  onclick={() => onselect?.(index)}>{moves[index].san}</button>
        </span>
      {/if}
    {/each}
    {#each r.rejected as rej}
      <span></span>
      <span class="rejected">✗ {rej.side} tried {rej.move} ({rej.reason})</span>
    {/each}
  {:else}
    <span class="faint" style="grid-column: 1 / 4">No moves yet.</span>
  {/each}
</div>
