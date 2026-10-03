<script lang="ts" module>
  export type TermStatus = 'connecting' | 'open' | 'error' | 'closed';
</script>

<script lang="ts">
  /**
   * Live, interactive xterm.js relay of a `tmux attach` PTY (via `@awm/client`'s
   * `openTerminal`). Views AND drives the pane; closing detaches, never kills.
   *
   * Generalized from tts-history's TmuxTerminal: the attach target is either a
   * registry agent's `scope` or an ad-hoc `{tmux_session}` name (machine-wide
   * roster row). Exposes an imperative `send()` so the surrounding chrome
   * (compose box, command grid) can inject keystrokes/control bytes. `interactive`
   * gates whether *raw xterm keystrokes* pass through — default off so a mobile
   * fat-finger can't type into a live agent until the user takes control.
   *
   * Mount only while VISIBLE and laid out — xterm measures its container on open.
   */
  import { onDestroy } from 'svelte';
  import { Terminal } from '@xterm/xterm';
  import { FitAddon } from '@xterm/addon-fit';
  import '@xterm/xterm/css/xterm.css';
  import { openTerminal, type TerminalSession, type TerminalTarget } from '@awm/client';

  interface Props {
    /** Attach target: a unit slug (string) or {scope|tmux_session}. */
    target: string | TerminalTarget;
    /** Pass raw xterm keystrokes to the pane (take-control). */
    interactive?: boolean;
    onStatus?: (s: TermStatus) => void;
  }
  let { target, interactive = false, onStatus }: Props = $props();

  // Mirror `interactive` into a ref the (closure-captured) onData reads live.
  // When raw typing is enabled, pull focus so physical keystrokes land on the
  // pane without a click.
  const gate = { on: false };
  $effect(() => {
    gate.on = interactive;
    if (interactive) { try { term?.focus(); } catch { /* ignore */ } }
  });

  let host: HTMLDivElement | undefined = $state();
  let term: Terminal | null = null;
  let fit: FitAddon | null = null;
  let sess: TerminalSession | null = null;
  let ro: ResizeObserver | null = null;
  let status = $state<TermStatus>('connecting');
  let errMsg = $state('');

  function setStatus(s: TermStatus): void {
    status = s;
    onStatus?.(s);
  }

  /** Inject bytes into the pane regardless of the raw-keystroke gate. */
  export function send(data: string): void {
    try { sess?.send(data); } catch { /* ignore */ }
  }
  export function focusTerm(): void {
    try { term?.focus(); } catch { /* ignore */ }
  }

  function teardown(): void {
    try { ro?.disconnect(); } catch { /* ignore */ }
    ro = null;
    try { sess?.close(); } catch { /* ignore */ }
    sess = null;
    try { term?.dispose(); } catch { /* ignore */ }
    term = null;
    fit = null;
  }

  function start(el: HTMLDivElement, tgt: string | TerminalTarget): void {
    setStatus('connecting');
    errMsg = '';
    const t = new Terminal({
      convertEol: false,
      cursorBlink: true,
      // xterm can't resolve CSS var() in fontFamily (it writes the string straight
      // to the renderer), so pass a concrete monospace stack — otherwise it fell
      // back to a mismatched default font.
      fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, ' +
        '"DejaVu Sans Mono", "Liberation Mono", monospace',
      fontSize: 13,
      // A relayed tmux/TUI pane manages its own screen; keep no client scrollback
      // so xterm never grows a viewport scrollbar down the right edge.
      scrollback: 0,
      theme: { background: '#101010', foreground: '#d6d6d6' },
    });
    const f = new FitAddon();
    t.loadAddon(f);
    t.open(el);
    f.fit();
    term = t;
    fit = f;
    // Re-fit once layout has settled — the first fit can run before the flex
    // body has its final height, which clips the top/bottom rows.
    requestAnimationFrame(() => {
      if (fit === f && term === t) {
        try { f.fit(); sess?.resize(t.cols, t.rows); } catch { /* ignore */ }
      }
    });

    sess = openTerminal(tgt, { cols: t.cols, rows: t.rows }, {
      onData: (bytes) => term?.write(bytes),
      onError: (m) => { setStatus('error'); errMsg = m; },
      onClose: () => { if (status !== 'error') setStatus('closed'); },
    });
    setStatus('open');

    // Raw keystrokes → pane, but only when the caller has taken control.
    t.onData((d) => { if (gate.on) sess?.send(d); });
    if (gate.on) { try { t.focus(); } catch { /* ignore */ } }

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

  // Re-open on mount and whenever the identity changes. Keyed on a stable string
  // so an object target doesn't re-trigger every render.
  const key = $derived(typeof target === 'string' ? target : JSON.stringify(target));
  $effect(() => {
    const el = host;
    void key; // subscribe to identity changes
    const tgt = target;
    teardown();
    if (el) {
      queueMicrotask(() => {
        if (host === el) start(el, tgt);
      });
    }
    return teardown;
  });

  onDestroy(teardown);
</script>

<div class="term">
  {#if status === 'error'}
    <div class="term-msg term-err">terminal unavailable — {errMsg}</div>
  {:else if status === 'closed'}
    <div class="term-msg">terminal detached</div>
  {/if}
  <div class="term-host" bind:this={host}></div>
</div>

<style>
  .term {
    position: relative;
    display: flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-height: 0;
    background: #101010;
  }
  .term-host {
    flex: 1 1 auto;
    min-height: 0;
    padding: 4px 6px;
    overflow: hidden;
  }
  /* A relayed TUI owns its screen; suppress xterm's own viewport scrollbar. */
  .term-host :global(.xterm-viewport) { scrollbar-width: none; overflow-y: hidden; }
  .term-host :global(.xterm-viewport::-webkit-scrollbar) { display: none; }
  .term-msg {
    flex: 0 0 auto;
    padding: 4px 8px;
    font-family: var(--mono, monospace);
    font-size: 0.8rem;
    color: var(--text3, #888);
    border-bottom: 1px solid var(--border, #333);
  }
  .term-err { color: var(--danger, #e06c75); }
  .term-host :global(.xterm) { height: 100%; }
</style>
