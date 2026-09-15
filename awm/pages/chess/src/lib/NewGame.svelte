<script lang="ts">
  import { Button } from '@awm/primitives';
  import { call, randomColor, type RealmError } from './api';

  interface Props {
    mode: 'pvb' | 'bvb';
    bots: string[];
    onclose: () => void;
    onstarted: (sid: string) => void;
  }
  let { mode = $bindable(), bots, onclose, onstarted }: Props = $props();

  const HUMAN = '@human';
  const preferred = (name: string) => (bots.includes(name) ? name : bots[0] ?? '');

  let opponent = $state(preferred('simple'));
  let side = $state<'white' | 'black' | 'random'>('white');
  let white = $state(preferred('simple'));
  let black = $state(preferred('naive'));
  let games = $state(1);
  let moveTimeout = $state(5);
  let pace = $state(0.8);
  let advanced = $state(false);
  let fen = $state('');
  let seed = $state('');
  let maxPlies = $state('');
  let starting = $state(false);
  let error = $state<string | null>(null);

  async function start(e: SubmitEvent) {
    e.preventDefault();
    const opts: Record<string, unknown> = { games, move_timeout_s: moveTimeout };
    if (mode === 'pvb') {
      const color = side === 'random' ? randomColor() : side;
      opts.white = color === 'white' ? HUMAN : opponent;
      opts.black = color === 'black' ? HUMAN : opponent;
    } else {
      opts.white = white;
      opts.black = black;
      opts.min_ply_s = pace;
    }
    if (fen.trim()) opts.fen = fen.trim();
    if (seed.trim()) opts.seed = Number(seed);
    if (maxPlies.trim()) opts.max_plies = Number(maxPlies);
    starting = true;
    error = null;
    try {
      const { session_id } = await call<{ session_id: string }>('acquire', { game: 'chess', opts });
      onstarted(session_id);
    } catch (err) {
      error = (err as RealmError).message;
    } finally {
      starting = false;
    }
  }
</script>

<div class="modal-backdrop" role="presentation" onclick={(e) => e.target === e.currentTarget && onclose()}>
  <form class="modal" onsubmit={start}>
    <h2>New game</h2>

    <div class="segmented">
      <Button kind={mode === 'pvb' ? 'primary' : 'ghost'} onclick={() => (mode = 'pvb')}>Play vs bot</Button>
      <Button kind={mode === 'bvb' ? 'primary' : 'ghost'} onclick={() => (mode = 'bvb')}>Bots vs bots</Button>
    </div>

    {#if mode === 'pvb'}
      <div class="row">
        <label class="field"><span>Bot</span>
          <select bind:value={opponent}>{#each bots as bot}<option>{bot}</option>{/each}</select>
        </label>
        <label class="field"><span>You play</span>
          <select bind:value={side}>
            <option value="white">white</option>
            <option value="black">black</option>
            <option value="random">random</option>
          </select>
        </label>
      </div>
    {:else}
      <div class="row">
        <label class="field"><span>White</span>
          <select bind:value={white}>{#each bots as bot}<option>{bot}</option>{/each}</select>
        </label>
        <label class="field"><span>Black</span>
          <select bind:value={black}>{#each bots as bot}<option>{bot}</option>{/each}</select>
        </label>
      </div>
      <label class="field"><span>Pace: {pace.toFixed(1)} s between moves (0 = full speed)</span>
        <input type="range" min="0" max="3" step="0.1" bind:value={pace} />
      </label>
    {/if}

    <div class="row">
      <label class="field"><span>Games{games > 1 ? ' (colours alternate)' : ''}</span>
        <input type="number" min="1" max="100" bind:value={games} />
      </label>
      <label class="field"><span>Bot move time (s)</span>
        <input type="number" min="0.5" step="0.5" bind:value={moveTimeout} />
      </label>
    </div>

    <div>
      <Button size="sm" onclick={() => (advanced = !advanced)}>{advanced ? '▾' : '▸'} Advanced</Button>
    </div>
    {#if advanced}
      <label class="field"><span>Starting FEN</span>
        <input type="text" placeholder="standard start" bind:value={fen} />
      </label>
      <div class="row">
        <label class="field"><span>Seed</span><input type="text" inputmode="numeric" bind:value={seed} /></label>
        <label class="field"><span>Max plies</span><input type="text" inputmode="numeric" placeholder="500" bind:value={maxPlies} /></label>
      </div>
    {/if}

    {#if error}<div class="error">{error}</div>{/if}

    <div class="controls">
      <Button kind="primary" type="submit" disabled={starting || !bots.length}>
        {starting ? 'Starting…' : 'Start'}
      </Button>
      <Button onclick={onclose}>Cancel</Button>
    </div>
  </form>
</div>
