<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { fetchSlashCommands } from './api';
  import type { SlashCommandInfo } from './types';

  interface Props {
    roomId: string;
    scope: string | null;
    onpick: (cmd: string, autorun: boolean) => void;
    onclose: () => void;
  }
  let { roomId, scope, onpick, onclose }: Props = $props();

  // Argless server commands run immediately on selection. Anything with
  // `<x>` or `[x]` in its args gets prefilled into the composer so the user
  // can fill in the argument.
  const AUTORUN = new Set(['/restart', '/kill', '/compact', '/clear', '/help', '/plan', '/yolo']);

  type Row = { cmd: string; label: string; description: string; group: 'server' | 'claude' };

  let rows      = $state<Row[]>([]);
  let loading   = $state(true);
  let err       = $state<string>('');
  let query     = $state('');
  let cursor    = $state(0);
  let queryEl: HTMLInputElement | undefined = $state();

  const filtered = $derived(
    rows.filter(r => !query || r.cmd.toLowerCase().includes(query.toLowerCase()))
  );

  onMount(async () => {
    await load();
    await tick();
    queryEl?.focus();
  });

  async function load() {
    loading = true;
    err = '';
    try {
      if (!scope) {
        // No live manager — fall back to server catalog only via a probe.
        // Backend requires a scope arg; without one, show the static
        // server list (rendered from the same SlashCommandInfo shape).
        rows = STATIC_SERVER.map(c => ({
          cmd: c.name, label: `${c.name} ${c.args}`.trim(),
          description: c.description, group: 'server' as const
        }));
      } else {
        const res = await fetchSlashCommands(roomId, scope);
        const server: Row[] = res.server.map(c => ({
          cmd: c.name, label: `${c.name} ${c.args}`.trim(),
          description: c.description, group: 'server' as const
        }));
        const claude: Row[] = (res.claude ?? []).map(name => ({
          cmd: `/${name}`, label: `/${name}`,
          description: 'claude built-in', group: 'claude' as const
        }));
        rows = [...server, ...claude];
      }
    } catch (e) {
      err = (e as Error).message;
      rows = [];
    } finally {
      loading = false;
      cursor = 0;
    }
  }

  function pickAt(i: number) {
    const row = filtered[i];
    if (!row) return;
    onpick(row.cmd, AUTORUN.has(row.cmd));
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape')      { e.preventDefault(); onclose(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); cursor = Math.min(cursor + 1, filtered.length - 1); }
    else if (e.key === 'ArrowUp')   { e.preventDefault(); cursor = Math.max(cursor - 1, 0); }
    else if (e.key === 'Enter')     { e.preventDefault(); pickAt(cursor); }
  }

  // Mirrors awm/services/agent_slash.py _COMMANDS — fallback when no scope.
  const STATIC_SERVER: SlashCommandInfo[] = [
    { name: '/restart', args: '[mode]', description: 'Respawn the agent, preserving conversation.' },
    { name: '/kill',    args: '',       description: 'SIGKILL the agent.' },
    { name: '/mode',    args: '<mode>', description: 'Respawn with permission-mode.' },
    { name: '/plan',    args: '',       description: 'Shortcut for /mode plan.' },
    { name: '/yolo',    args: '',       description: 'Shortcut for /mode bypassPermissions.' },
    { name: '/model',   args: '<name>', description: 'Respawn with --model.' },
    { name: '/effort',  args: '<level>',description: 'Respawn with --effort.' },
    { name: '/compact', args: '',       description: 'Not supported in headless mode (REPL-only).' },
    { name: '/clear',   args: '',       description: 'Wipe context: respawn without --resume.' },
    { name: '/help',    args: '',       description: 'Echo the catalog.' },
  ];
</script>

<div class="picker" onkeydown={onKey} role="listbox" tabindex="-1">
  <header class="head">
    <input
      bind:this={queryEl}
      class="filter mono"
      type="text"
      placeholder="filter commands…"
      bind:value={query}
      autocomplete="off"
    />
    <button class="close mono" type="button" onclick={onclose} aria-label="close">×</button>
  </header>
  {#if loading}
    <div class="row dim mono">loading…</div>
  {:else if err}
    <div class="row danger mono">{err}</div>
  {:else if filtered.length === 0}
    <div class="row dim mono">no matches</div>
  {:else}
    <ul class="list">
      {#each filtered as r, i (r.group + ':' + r.cmd)}
        <li
          class="row"
          class:active={i === cursor}
          class:group-claude={r.group === 'claude'}
          role="option"
          aria-selected={i === cursor}
          onmouseenter={() => (cursor = i)}
          onclick={() => pickAt(i)}
          onkeydown={onKey}
        >
          <span class="cmd mono">{r.label}</span>
          <span class="desc">{r.description}</span>
          <span class="badge mono">{r.group === 'server' ? 'srv' : 'cl'}</span>
        </li>
      {/each}
    </ul>
  {/if}
  {#if !scope && !loading}
    <footer class="foot dim mono">no live manager — server commands only</footer>
  {/if}
</div>

<style>
  .picker {
    position: absolute;
    bottom: calc(100% + 6px);
    left: 14px;
    right: 14px;
    max-height: 320px;
    display: flex;
    flex-direction: column;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    box-shadow: 0 -4px 12px rgba(0,0,0,0.4);
    z-index: 20;
    overflow: hidden;
  }
  .head {
    display: flex;
    align-items: stretch;
    border-bottom: 1px solid var(--border);
    background: var(--surface2, var(--surface));
  }
  .filter {
    flex: 1;
    background: transparent;
    border: 0;
    color: var(--text);
    padding: 8px 10px;
    font-size: 12px;
    outline: none;
  }
  .close {
    background: transparent;
    border: 0;
    color: var(--text3);
    width: 32px;
    font-size: 16px;
    cursor: pointer;
  }
  .close:hover { color: var(--text); }

  .list {
    list-style: none;
    margin: 0;
    padding: 4px 0;
    overflow-y: auto;
    flex: 1;
  }
  .row {
    display: grid;
    grid-template-columns: minmax(120px, max-content) 1fr auto;
    gap: 10px;
    align-items: baseline;
    padding: 6px 12px;
    cursor: pointer;
    border-left: 2px solid transparent;
  }
  .row.active {
    background: color-mix(in oklab, var(--atomizer) 12%, transparent);
    border-left-color: var(--atomizer);
  }
  .row .cmd  { color: var(--text); font-size: 12px; }
  .row .desc { color: var(--text2); font-size: 11px; }
  .row .badge {
    color: var(--text3);
    font-size: 9px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
  }
  .row.group-claude .cmd { color: var(--warn); }
  .row.dim   { color: var(--text3); padding: 10px 12px; }
  .row.danger { color: var(--danger); padding: 10px 12px; }
  .foot {
    padding: 6px 12px;
    border-top: 1px solid var(--border);
    color: var(--text3);
    font-size: 10px;
    letter-spacing: 0.5px;
  }

  @media (max-width: 720px) {
    .picker { left: 8px; right: 8px; max-height: 50vh; }
    .row { grid-template-columns: minmax(100px, max-content) 1fr; }
    .row .badge { display: none; }
  }
</style>
