<script lang="ts">
  // Read-only replay of one game from a save or a session record. The realm sends
  // a FEN per ply, so scrubbing needs no further calls.
  import { onMount } from 'svelte';
  import { Button } from '@awm/primitives';
  import Board from './Board.svelte';
  import MoveList from './MoveList.svelte';
  import { call, go, isExternal, REASONS, type Color, type RealmError } from './api';

  let { save, session, game }: { save: string | null; session: string | null; game: string | null } = $props();

  let data = $state<any>(null);
  let summary = $state<any>(null);
  let cursor = $state(-1);
  let flipped = $state(false);
  let resuming = $state(false);
  let error = $state<string | null>(null);

  const last = $derived(data ? data.moves.length - 1 : -1);
  const fen = $derived(!data ? '' : cursor < 0 ? data.initial_fen : data.moves[cursor].fen);
  const lastMove = $derived(data && cursor >= 0 ? data.moves[cursor].uci : null);
  const players = $derived<Record<string, string>>(data?.players ?? {});
  const orientation = $derived.by<Color>(() => {
    const base: Color = isExternal(players.black) && !isExternal(players.white) ? 'black' : 'white';
    return flipped ? (base === 'white' ? 'black' : 'white') : base;
  });
  const source = $derived(save ?? `session:${session}`);

  onMount(() => {
    void load();
    const onKey = (e: KeyboardEvent) => {
      if (!data || (e.target as HTMLElement | null)?.closest?.('input, select, textarea')) return;
      if (e.key === 'ArrowLeft') cursor = Math.max(-1, cursor - 1);
      else if (e.key === 'ArrowRight') cursor = Math.min(last, cursor + 1);
      else if (e.key === 'Home') cursor = -1;
      else if (e.key === 'End') cursor = last;
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  async function load() {
    try {
      data = await call('replay', save ? { name: save, game_id: game } : { session_id: session, game_id: game });
      cursor = data.moves.length - 1;
      if (save) summary = (await call('saves')).saves.find((s: any) => s.name === save) ?? null;
    } catch (err) {
      error = (err as RealmError).message;
    }
  }

  async function resume() {
    resuming = true;
    error = null;
    try {
      const { session_id } = await call('acquire', { game: 'chess', opts: { load: save } });
      go({ sid: session_id });
    } catch (err) {
      error = (err as RealmError).message;
    } finally {
      resuming = false;
    }
  }
</script>

{#if error}<div class="error">{error}</div>{/if}

{#if data}
  <div class="game">
    <div>
      <Board {fen} {orientation} {lastMove} />
    </div>

    <div class="side">
      <div class="card">
        <p class="muted" style="margin-bottom: var(--space-3)">
          Replay of {save ? `save ${save}` : `session ${session}`}
        </p>
        {#each [orientation === 'white' ? 'black' : 'white', orientation] as color (color)}
          <div class="player">
            <span class="swatch {color}"></span>
            <span class="name">{isExternal(players[color]) ? 'you' : players[color] ?? '—'}</span>
          </div>
        {/each}
        {#if data.game_ids.length > 1}
          <label class="field" style="margin-top: var(--space-3)">
            <span>Game</span>
            <select value={data.game_id}
                    onchange={(e) => go({ replay: source, game: e.currentTarget.value })}>
              {#each data.game_ids as id, i (id)}<option value={id}>game {i + 1}</option>{/each}
            </select>
          </label>
        {/if}
        <p style="margin-top: var(--space-3)">
          {#if data.outcome}
            {data.outcome.result} · {REASONS[data.outcome.reason] ?? data.outcome.reason}
          {:else}
            <span class="muted">Unfinished game</span>
          {/if}
        </p>
      </div>

      <div class="card controls">
        <Button size="sm" aria-label="First position" onclick={() => (cursor = -1)}>«</Button>
        <Button size="sm" aria-label="Previous move" onclick={() => (cursor = Math.max(-1, cursor - 1))}>◀</Button>
        <Button size="sm" aria-label="Next move" onclick={() => (cursor = Math.min(last, cursor + 1))}>▶</Button>
        <Button size="sm" aria-label="Last position" onclick={() => (cursor = last)}>»</Button>
        <span class="muted">{cursor + 1} / {data.moves.length}</span>
        {#if cursor >= 0 && data.moves[cursor].think_s != null}
          <span class="faint">thought {data.moves[cursor].think_s.toFixed(2)} s</span>
        {/if}
        <Button size="sm" onclick={() => (flipped = !flipped)}>Flip</Button>
      </div>

      <div class="card">
        <MoveList moves={data.moves} rejected={data.rejected} current={cursor} onselect={(i) => (cursor = i)} />
      </div>

      <div class="controls">
        {#if save && summary && !summary.finished}
          <Button kind="primary" disabled={resuming} onclick={resume}>
            {resuming ? 'Resuming…' : 'Resume this save'}
          </Button>
        {/if}
        <Button onclick={() => go()}>Back to lobby</Button>
      </div>
    </div>
  </div>
{:else if !error}
  <p class="muted">Loading…</p>
{/if}
