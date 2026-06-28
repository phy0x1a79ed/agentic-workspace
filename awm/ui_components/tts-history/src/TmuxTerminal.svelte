<script lang="ts">
  /**
   * Live, interactive view of a claude-tmux agent's terminal.
   *
   * Paints the agent's real `claude` TUI with xterm.js and drives it: keystrokes
   * go back to the pane, output streams in, and the panel's size is forwarded to
   * the server (the agent is pure I/O text — the connected UI dictates the size).
   * The transport is `@awm/client`'s `openTerminal`, a byte relay of `tmux
   * attach`; closing detaches without killing the agent.
   *
   * Mount this only while it is VISIBLE (the host renders it behind a tab guard)
   * — xterm must fit in a laid-out container, so a hidden zero-size mount would
   * mis-size the grid. Switching the `slug` identity tears the session down and
   * re-opens for the new agent, resetting the buffer.
   */
  import { onDestroy } from 'svelte';
  import { Terminal } from '@xterm/xterm';
  import { FitAddon } from '@xterm/addon-fit';
  import '@xterm/xterm/css/xterm.css';
  import { openTerminal, type TerminalSession } from '@awm/client';

  interface Props {
    /** The agent identity (unit slug) to attach to. Re-attaches when it changes. */
    slug: string;
  }
  let { slug }: Props = $props();

  let host: HTMLDivElement | undefined = $state();
  let term: Terminal | null = null;
  let fit: FitAddon | null = null;
  let sess: TerminalSession | null = null;
  let ro: ResizeObserver | null = null;
  let status = $state<'connecting' | 'open' | 'error' | 'closed'>('connecting');
  let errMsg = $state('');

  function teardown(): void {
    try { ro?.disconnect(); } catch { /* ignore */ }
    ro = null;
    try { sess?.close(); } catch { /* ignore */ }
    sess = null;
    try { term?.dispose(); } catch { /* ignore */ }
    term = null;
    fit = null;
  }

  function start(el: HTMLDivElement, s: string): void {
    status = 'connecting';
    errMsg = '';
    const t = new Terminal({
      convertEol: false,
      cursorBlink: true,
      fontFamily: 'var(--mono, ui-monospace, monospace)',
      fontSize: 13,
      theme: { background: '#101010', foreground: '#d6d6d6' },
    });
    const f = new FitAddon();
    t.loadAddon(f);
    t.open(el);
    f.fit();
    term = t;
    fit = f;

    sess = openTerminal(s, { cols: t.cols, rows: t.rows }, {
      onData: (bytes) => term?.write(bytes),
      onError: (m) => { status = 'error'; errMsg = m; },
      onClose: () => { if (status !== 'error') status = 'closed'; },
    });
    status = 'open';

    // Keystrokes → pane.
    t.onData((d) => sess?.send(d));

    // Panel resize → fit the grid and tell the server (UI dictates size).
    const obs = new ResizeObserver(() => {
      if (!fit || !term) return;
      try {
        fit.fit();
        sess?.resize(term.cols, term.rows);
      } catch { /* ignore transient zero-size */ }
    });
    obs.observe(el);
    ro = obs;
  }

  // (Re)open on mount and whenever the identity changes. The open runs in a
  // microtask so its $state writes don't subscribe this effect to itself, and so
  // the host element is laid out before xterm measures it.
  $effect(() => {
    const el = host;
    const s = slug;
    teardown();
    if (el && s) {
      queueMicrotask(() => {
        if (host === el && slug === s) start(el, s);
      });
    }
    return teardown;
  });

  onDestroy(teardown);
</script>

<div class="tmux-terminal">
  {#if status === 'error'}
    <div class="tmux-msg tmux-err">terminal unavailable — {errMsg}</div>
  {:else if status === 'closed'}
    <div class="tmux-msg">terminal detached</div>
  {/if}
  <div class="tmux-host" bind:this={host}></div>
</div>

<style>
  .tmux-terminal {
    position: relative;
    display: flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-height: 0;
    background: #101010;
  }
  .tmux-host {
    flex: 1 1 auto;
    min-height: 0;
    padding: 6px 8px;
    overflow: hidden;
  }
  .tmux-msg {
    flex: 0 0 auto;
    padding: 4px 8px;
    font-family: var(--mono, monospace);
    font-size: 0.8rem;
    color: var(--text3, #888);
    border-bottom: 1px solid var(--border, #333);
  }
  .tmux-err { color: var(--danger, #e06c75); }
  /* xterm fills the host. */
  .tmux-host :global(.xterm) { height: 100%; }
</style>
