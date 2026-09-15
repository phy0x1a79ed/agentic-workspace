<script lang="ts">
  // A live session: board, players, controls. The view is a snapshot patched by
  // `chess` emitter events in seq order; any gap, resync or reconnect re-snapshots.
  import { onMount } from 'svelte';
  import { Button } from '@awm/primitives';
  import Board from './Board.svelte';
  import MoveList from './MoveList.svelte';
  import { call, go, subscribe, REASONS, type ChessEvent, type Color, type RealmError } from './api';

  let { sid }: { sid: string } = $props();

  let status = $state<any>(null);
  let pos = $state<any>(null);
  let history = $state<any>(null);
  let ended = $state(false);
  let error = $state<string | null>(null);
  let notice = $state<string | null>(null);
  let submitting = $state(false);
  let flipped = $state(false);
  let pace = $state(0);
  let boardRef = $state<any>();

  let seq = -1;
  let resyncing = false;
  let buffered: ChessEvent[] = [];

  const bots = $derived<any[]>(status?.bots ?? []);
  const byColor = (c: Color) => bots.find((b) => b.color === c);
  const externals = $derived(bots.filter((b) => b.external));
  const humanColor = $derived<Color | null>(externals.length === 1 ? externals[0].color : null);
  const spectating = $derived(externals.length === 0);
  const awaiting = $derived(pos?.awaiting ?? null);
  const myTurn = $derived(!!awaiting?.external && !submitting && status?.phase === 'running');
  const orientation = $derived.by<Color>(() => {
    const base = humanColor ?? 'white';
    return flipped ? (base === 'white' ? 'black' : 'white') : base;
  });
  const topColor = $derived<Color>(orientation === 'white' ? 'black' : 'white');
  const finished = $derived(status?.phase === 'finished');

  onMount(() => {
    let wasLive = true;
    const follow = () => subscribe(onEvent, (live) => {
      if (live && !wasLive) void resync();
      wasLive = live;
    });
    let stop = follow();
    // When another service instance takes over the prefix, the gateway can leave
    // this socket open but silent, so compare seq and re-follow on a gap.
    const watchdog = setInterval(async () => {
      if (ended || resyncing || !status) return;
      try {
        const snap = await call('snapshot', { session_id: sid });
        if (snap.seq > seq) {
          stop();
          stop = follow();
          void resync();
        }
      } catch { /* resync reports a dead session */ }
    }, 10000);
    void resync();
    return () => {
      clearInterval(watchdog);
      stop();
    };
  });

  async function resync() {
    if (resyncing) return;
    resyncing = true;
    try {
      const snap = await call('snapshot', { session_id: sid });
      status = snap.snapshot.status;
      pos = snap.snapshot.state;
      history = snap.snapshot.history ?? null;
      pace = status.min_ply_s ?? 0;
      seq = snap.seq;
      ended = false;
    } catch (err) {
      if ((err as RealmError).code === 'not_running') ended = true;
      else error = (err as RealmError).message;
    } finally {
      resyncing = false;
      const queued = buffered;
      buffered = [];
      for (const e of queued) apply(e);
    }
  }

  function onEvent(e: ChessEvent) {
    if (e.session_id !== sid) return;
    if (e.kind === 'session_ended') ended = true;
    else if (e.kind === 'saved') notice = `Saved as ${e.data.name}.`;
    else if (e.kind === 'resync') void resync();
    else if (e.seq !== null) {
      if (resyncing) buffered.push(e);
      else apply(e);
    }
  }

  function apply(e: ChessEvent) {
    if (e.seq === null || e.seq <= seq) return;
    if (!status || !pos || e.seq !== seq + 1) {
      void resync();
      return;
    }
    seq = e.seq;
    const d = e.data;
    switch (e.kind) {
      case 'phase':
        status.phase = d.phase;
        status.note = d.note;
        break;
      case 'game_start':
        status.game_id = d.game_id;
        status.game_index = d.index;
        for (const b of status.bots) b.color = b.slot === d.white_slot ? 'white' : 'black';
        history = { game_id: d.game_id, index: d.index, players: d.players,
                    initial_fen: d.initial_fen, moves: [], rejected: [], outcome: null };
        pos = { ...pos, game_id: d.game_id, fen: d.initial_fen, ply: 0, last_move: null,
                outcome: null, awaiting: null, legal_moves: [], players: d.players };
        break;
      case 'turn':
        pos.awaiting = { slot: d.slot, color: d.color, seat: d.seat, external: d.external,
                         game_id: d.game_id, ply: d.ply, since: d.since };
        pos.legal_moves = d.legal_moves;
        pos.fen = d.fen;
        pos.turn = d.color;
        break;
      case 'move':
        history?.moves.push({ ply: d.ply, side: d.side, uci: d.uci, san: d.san,
                              think_s: d.think_s, fen: d.fen });
        pos.fen = d.fen;
        pos.ply = d.ply + 1;
        pos.last_move = d.uci;
        pos.awaiting = null;
        pos.legal_moves = [];
        break;
      case 'rejected':
        history?.rejected.push(d);
        break;
      case 'game_over':
        pos.outcome = { result: d.result, reason: d.reason };
        pos.awaiting = null;
        pos.legal_moves = [];
        status.score = d.score;
        for (const b of status.bots) b.points = d.score[b.slot];
        if (history) history.outcome = pos.outcome;
        break;
      case 'pace':
        status.min_ply_s = d.min_ply_s;
        pace = d.min_ply_s;
        break;
    }
  }

  async function act(verb: string, args: Record<string, unknown> = {}): Promise<any> {
    error = null;
    try {
      return await call(verb, { session_id: sid, ...args });
    } catch (err) {
      error = (err as RealmError).message;
      if ((err as RealmError).code === 'not_running') ended = true;
      return null;
    }
  }

  async function move(uci: string) {
    if (!awaiting) return;
    const ply = awaiting.ply;
    submitting = true;
    error = null;
    try {
      const res = await call('move', { session_id: sid, move: uci, ply });
      if (res.accepted) {
        if (pos.awaiting?.ply === ply) {
          pos.awaiting = null;
          pos.legal_moves = [];
        }
      } else {
        error = `${uci} is ${res.reason}.`;
        boardRef?.redraw();
      }
    } catch (err) {
      const e = err as RealmError;
      error = e.message;
      boardRef?.redraw();
      if (e.code === 'stale' || e.code === 'not_your_turn') void resync();
    } finally {
      submitting = false;
    }
  }

  async function save() {
    const players = [byColor('white'), byColor('black')].map((b) => b?.name ?? 'x').join('-vs-');
    const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
    const name = window.prompt('Save as', `${players}-${stamp}`.replace(/[^A-Za-z0-9_.-]/g, ''));
    if (!name) return;
    if (!/^[A-Za-z0-9_.-]+$/.test(name) || name.startsWith('autosave-')) {
      error = 'Use letters, digits, "_", "." and "-", and do not start with autosave-.';
      return;
    }
    error = null;
    try {
      await call('save', { session_id: sid, name });
    } catch (err) {
      const e = err as RealmError;
      if (e.code !== 'exists') error = e.message;
      else if (window.confirm(`A save named ${name} exists. Overwrite it?`)) await act('save', { name, overwrite: true });
    }
  }

  async function resign() {
    if (window.confirm('Resign this game?')) await act('resign');
  }

  async function end() {
    if (!window.confirm('End this session? Its record stays available to replay.')) return;
    if (await act('release')) go();
  }

  async function rematch() {
    const res = await act('reset');
    if (res) go({ sid: res.session_id });
  }

  const label = (b: any) => (!b ? '—' : b.external && humanColor ? 'you' : b.name);

  function note(b: any): string {
    if (!b || !pos || pos.outcome || !awaiting || awaiting.slot !== b.slot) return '';
    if (!b.external) return 'thinking…';
    return humanColor ? 'your move' : 'to move';
  }

  function outcomeText(o: { result: string; reason: string }): string {
    const winner = o.result === '1-0' ? byColor('white') : o.result === '0-1' ? byColor('black') : null;
    const who = winner ? (label(winner) === 'you' ? 'You win' : `${label(winner)} wins`) : 'Draw';
    return `${who} · ${o.result} · ${REASONS[o.reason] ?? o.reason}`;
  }
</script>

{#if error}<div class="error">{error}</div>{/if}

{#if ended}
  <div class="banner" style="margin-bottom: var(--space-4)">
    <p>This session has ended.</p>
    <div class="controls" style="margin-top: var(--space-3)">
      <Button kind="primary" onclick={() => go({ replay: `session:${sid}`, game: pos?.game_id })}>Replay it</Button>
      <Button onclick={rematch}>Rematch</Button>
      <Button onclick={() => go()}>Back to lobby</Button>
    </div>
  </div>
{/if}

{#if pos && status}
  <div class="game">
    <div>
      <Board bind:this={boardRef} fen={pos.fen} {orientation} lastMove={pos.last_move}
             legalMoves={myTurn ? pos.legal_moves : []} inputColor={myTurn ? awaiting.color : null}
             onmove={move} />
    </div>

    <div class="side">
      <div class="card">
        {#each [topColor, orientation] as color (color)}
          {@const b = byColor(color)}
          <div class="player" class:to-move={!!b && awaiting?.slot === b.slot}>
            <span class="swatch {color}"></span>
            <span class="name">{label(b)}</span>
            <span class="faint">{b ? b.points : ''}</span>
            <span class="note">{note(b)}</span>
          </div>
        {/each}
        <p class="muted" style="margin-top: var(--space-3)">
          Session {sid} · game {status.game_index} of {status.games} · {status.phase}
          {#if status.note}· {status.note}{/if}
        </p>
      </div>

      {#if pos.outcome}
        <div class="banner">
          <p>{outcomeText(pos.outcome)}</p>
          {#if finished}
            <div class="controls" style="margin-top: var(--space-3)">
              <Button kind="primary" onclick={rematch}>Rematch</Button>
              <Button onclick={() => go({ replay: `session:${sid}`, game: pos.game_id })}>Replay</Button>
              <Button onclick={() => go()}>Back to lobby</Button>
            </div>
          {:else if status.phase === 'running'}
            <p class="muted">The next game starts shortly.</p>
          {/if}
        </div>
      {/if}

      <div class="card controls">
        {#if status.phase === 'running' && spectating}
          <Button onclick={() => act('pause')}>Pause</Button>
        {:else if status.phase === 'stopped'}
          <Button kind="primary" onclick={() => act('resume')}>Resume</Button>
          {#if spectating}<Button onclick={() => act('step')}>Step</Button>{/if}
        {/if}
        {#if externals.length && !finished && !pos.outcome}
          <Button kind="danger" onclick={resign}>Resign</Button>
        {/if}
        <Button onclick={save} disabled={!history?.moves.length}>Save</Button>
        <Button onclick={() => (flipped = !flipped)}>Flip</Button>
        <Button onclick={() => go()}>Leave</Button>
        <Button kind="danger" size="sm" onclick={end}>End session</Button>
      </div>

      {#if spectating}
        <label class="card field">
          <span>Pace: {pace.toFixed(1)} s between moves</span>
          <input type="range" min="0" max="3" step="0.1" bind:value={pace}
                 onchange={() => act('set_pace', { min_ply_s: pace })} />
        </label>
      {/if}

      {#if notice}<p class="ok">{notice}</p>{/if}

      <div class="card">
        <MoveList moves={history?.moves ?? []} rejected={history?.rejected ?? []} follow />
      </div>
    </div>
  </div>
{:else if !ended}
  <p class="muted">Connecting to session {sid}…</p>
{/if}
