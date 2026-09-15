<script lang="ts">
  import { onMount } from 'svelte';
  import { Button } from '@awm/primitives';
  import NewGame from './NewGame.svelte';
  import { call, go, subscribe, REASONS, type RealmError } from './api';

  let sessions = $state<any[]>([]);
  let saves = $state<any[]>([]);
  let records = $state<any[]>([]);
  let bots = $state<string[]>([]);
  let loaded = $state(false);
  let error = $state<string | null>(null);
  let newGame = $state(false);
  let mode = $state<'pvb' | 'bvb'>('pvb');
  let resuming = $state<string | null>(null);

  const REFRESH_KINDS = new Set(['session_started', 'session_ended', 'saved', 'game_start',
                                 'game_over', 'phase']);

  const liveSids = $derived(new Set(sessions.map((s) => s.sid)));
  const finished = $derived(records.filter((r) => !liveSids.has(r.name)));

  onMount(() => {
    void refresh();
    let timer: ReturnType<typeof setTimeout> | null = null;
    const stop = subscribe((e) => {
      if (!REFRESH_KINDS.has(e.kind)) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => { timer = null; void refresh(); }, 400);
    });
    const poll = setInterval(() => void refresh(), 15000);
    return () => {
      stop();
      clearInterval(poll);
      if (timer) clearTimeout(timer);
    };
  });

  async function refresh() {
    try {
      const [status, listing, botList] = await Promise.all([call('status'), call('saves'), call('bots')]);
      sessions = status.sessions;
      saves = listing.saves;
      records = listing.records;
      bots = botList.bots;
      error = null;
    } catch (err) {
      error = (err as RealmError).message;
    } finally {
      loaded = true;
    }
  }

  function start(kind: 'pvb' | 'bvb') {
    mode = kind;
    newGame = true;
  }

  async function resume(name: string) {
    resuming = name;
    error = null;
    try {
      const { session_id } = await call('acquire', { game: 'chess', opts: { load: name } });
      go({ sid: session_id });
    } catch (err) {
      error = (err as RealmError).message;
    } finally {
      resuming = null;
    }
  }

  const seat = (name: string | null | undefined) => (name?.startsWith('@') ? 'you' : name ?? '—');

  function seats(s: any): [any, any] {
    if (!s.bots.some((b: any) => b.color)) return [s.bots[0], s.bots[1]];
    return [s.bots.find((b: any) => b.color === 'white'), s.bots.find((b: any) => b.color === 'black')];
  }

  function result(summary: any): string {
    const o = summary.current?.outcome;
    if (o) return `${o.result} ${REASONS[o.reason] ?? o.reason}`;
    return summary.current ? 'unfinished' : '—';
  }

  const when = (iso: string | null) => (iso ? iso.replace('T', ' ') : '—');
</script>

<div class="controls" style="margin-bottom: var(--space-5)">
  <Button kind="primary" disabled={!bots.length} onclick={() => start('pvb')}>Play a bot</Button>
  <Button disabled={!bots.length} onclick={() => start('bvb')}>Watch bots</Button>
  <Button size="sm" onclick={refresh}>Refresh</Button>
</div>

{#if error}<div class="error">{error}</div>{/if}

<section>
  <h2>Live sessions</h2>
  {#if sessions.length}
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>session</th><th>white</th><th>black</th><th>mode</th><th>phase</th><th>game</th><th>score</th></tr>
        </thead>
        <tbody>
          {#each sessions as s (s.sid)}
            {@const [w, b] = seats(s)}
            <tr class="clickable" tabindex="0" onclick={() => go({ sid: s.sid })}
                onkeydown={(e) => e.key === 'Enter' && go({ sid: s.sid })}>
              <td>{s.sid}</td>
              <td>{seat(w?.name)}</td>
              <td>{seat(b?.name)}</td>
              <td>
                {#if s.labels?.mode === 'pvb'}<span class="badge you">you play</span>
                {:else}<span class="badge">bots</span>{/if}
              </td>
              <td><span class="badge {s.phase}">{s.phase}</span></td>
              <td>{s.game_index} / {s.games}</td>
              <td>{w?.points ?? 0} – {b?.points ?? 0}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if loaded}
    <p class="faint">No live sessions. Start one above.</p>
  {/if}
</section>

<section>
  <h2>Saves</h2>
  {#if saves.length}
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>name</th><th>saved</th><th>players</th><th>games</th><th>score</th><th>last game</th><th></th></tr>
        </thead>
        <tbody>
          {#each saves as s (s.name)}
            {#if s.error}
              <tr><td>{s.name}</td><td colspan="6" class="danger">{s.error}</td></tr>
            {:else}
              <tr>
                <td>{s.name}</td>
                <td class="muted">{when(s.saved_at)}</td>
                <td>{seat(s.players[0])} vs {seat(s.players[1])}</td>
                <td>{s.played} / {s.games}</td>
                <td>{s.score.join(' – ')}</td>
                <td>{result(s)}</td>
                <td class="actions">
                  <Button size="sm" kind="primary" disabled={s.finished || resuming !== null}
                          onclick={() => resume(s.name)}>{resuming === s.name ? '…' : 'Resume'}</Button>
                  <Button size="sm" disabled={!s.played} onclick={() => go({ replay: s.name })}>Replay</Button>
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
  {:else if loaded}
    <p class="faint">No saves yet.</p>
  {/if}
</section>

<section>
  <h2>Finished sessions</h2>
  {#if finished.length}
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>session</th><th>players</th><th>games</th><th>score</th><th>last game</th><th></th></tr>
        </thead>
        <tbody>
          {#each finished as r (r.name)}
            <tr>
              <td>{r.name}</td>
              <td>{seat(r.players[0])} vs {seat(r.players[1])}</td>
              <td>{r.played} / {r.games}</td>
              <td>{r.score.join(' – ')}</td>
              <td>{result(r)}</td>
              <td class="actions">
                <Button size="sm" disabled={!r.played} onclick={() => go({ replay: `session:${r.name}` })}>Replay</Button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if loaded}
    <p class="faint">No finished sessions.</p>
  {/if}
</section>

{#if newGame}
  <NewGame bind:mode {bots} onclose={() => (newGame = false)} onstarted={(sid) => go({ sid })} />
{/if}
