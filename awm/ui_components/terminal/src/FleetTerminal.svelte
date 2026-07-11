<script lang="ts">
  /**
   * Full-screen mobile terminal overlay: the core xterm relay plus the fleet
   * control chrome — a title bar with a back/detach control, a compose input
   * (type a turn + send), and a bottom action bar (detach · voice · keyboard ·
   * menu) whose menu opens a grid of control-byte commands (Esc, Ctrl-C,
   * /compact, /clear, arrows, ⇧Tab…).
   *
   * Everything the chrome sends routes through the core's imperative `send()`,
   * so the compose box and command grid work regardless of the raw-keystroke
   * passthrough (`interactive`) mode.
   *
   * Raw passthrough is ON by default: physical keystrokes go straight to the
   * tmux pane (the desktop-primary path). The keyboard toggle drops into a
   * "keys off" review mode where taps/keys don't reach the agent — and in that
   * mode the SPACEBAR is a push-to-talk that starts voice dictation.
   *
   * Voice uses the DEVICE-native Web Speech API (not the awm stt service). The
   * final transcript is sent straight to the pane (reaches the agent), with the
   * live partial shown in the compose box as feedback.
   */
  import Terminal from './Terminal.svelte';
  import type { TermStatus } from './Terminal.svelte';
  import { COMMAND_GRID, type TermCommand } from './commands';
  import { startDictation, voiceAvailable, type Dictation } from './voice';
  import type { TerminalTarget } from '@awm/client';

  interface Props {
    target: string | TerminalTarget;
    title?: string;
    subtitle?: string;
    /** When false, show a not-attachable notice instead of the terminal. */
    attachable?: boolean;
    /** Back / detach (close the overlay). Detaches; never kills. */
    onClose?: () => void;
  }
  let { target, title = 'agent', subtitle = '', attachable = true, onClose }: Props =
    $props();

  let core = $state<Terminal>();
  let interactive = $state(true);
  let compose = $state('');
  let menuOpen = $state(false);
  let status = $state<TermStatus>('connecting');
  let listening = $state(false);
  let dictation: Dictation | null = null;
  const canVoice = voiceAvailable();

  function sendCompose(): void {
    const text = compose;
    if (!text.trim()) return;
    core?.send(text + '\r');
    compose = '';
  }

  function runCommand(c: TermCommand): void {
    core?.send(c.bytes);
    menuOpen = false;
  }

  function toggleVoice(): void {
    if (listening) { dictation?.stop(); return; }
    dictation = startDictation({
      onInterim: (t) => { compose = t; }, // live preview only
      onFinal: (t) => {
        // Send the dictated turn straight to the agent, then clear the preview.
        const text = t.trim();
        if (text) core?.send(text + '\r');
        compose = '';
      },
      onEnd: () => { listening = false; dictation = null; },
    });
    listening = dictation !== null;
  }

  /** Keys-off review mode: SPACEBAR is push-to-talk. In typing mode space is a
   *  normal keystroke and goes to the pane, so this only fires when keys are off
   *  and focus isn't in a text field. */
  function onWindowKey(e: KeyboardEvent): void {
    if (interactive || !canVoice) return;
    if (e.code !== 'Space' && e.key !== ' ') return;
    const el = e.target as HTMLElement | null;
    const tag = el?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || el?.isContentEditable) return;
    e.preventDefault();
    toggleVoice();
  }

  const statusLabel = $derived(
    status === 'open' ? '● live'
    : status === 'connecting' ? '… connecting'
    : status === 'closed' ? 'detached'
    : 'error');
</script>

<svelte:window onkeydown={onWindowKey} />

<div class="fleet-term">
  <header class="ft-head">
    <button class="ft-back" onclick={() => onClose?.()} aria-label="detach and go back">‹</button>
    <div class="ft-title">
      <span class="ft-name">{title}</span>
      {#if subtitle}<span class="ft-sub">{subtitle}</span>{/if}
    </div>
    <span class="ft-status" class:live={status === 'open'} class:err={status === 'error'}>
      {statusLabel}
    </span>
  </header>

  {#if !attachable}
    <div class="ft-notattach">
      <p>This session isn't attachable.</p>
      <p class="ft-notattach-sub">No tmux handle was captured for it (it may not be
        running inside tmux). It still appears in the roster, but there's no live
        terminal to drive.</p>
    </div>
  {:else}
    <div class="ft-body">
      <Terminal bind:this={core} {target} {interactive} onStatus={(s) => (status = s)} />
    </div>

    {#if menuOpen}
      <div class="ft-grid" role="menu">
        {#each COMMAND_GRID as c (c.label)}
          <button class="ft-cmd" class:danger={c.danger} onclick={() => runCommand(c)}
                  title={c.hint}>
            <span class="ft-cmd-label">{c.label}</span>
            {#if c.hint}<span class="ft-cmd-hint">{c.hint}</span>{/if}
          </button>
        {/each}
      </div>
    {/if}

    <div class="ft-compose">
      <input
        class="ft-input"
        placeholder="Type a message…"
        bind:value={compose}
        onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); sendCompose(); } }}
      />
      <button class="ft-send" onclick={sendCompose} disabled={!compose.trim()}
              aria-label="send">↵</button>
    </div>

    <nav class="ft-bar">
      <button class="ft-act" onclick={() => onClose?.()}>
        <span class="ft-act-i">⤫</span><span class="ft-act-l">detach</span>
      </button>
      {#if canVoice}
        <button class="ft-act" class:on={listening} onclick={toggleVoice}>
          <span class="ft-act-i">{listening ? '◉' : '🎙'}</span>
          <span class="ft-act-l">{listening ? 'listening' : 'voice'}</span>
        </button>
      {/if}
      <button class="ft-act" class:on={interactive} onclick={() => (interactive = !interactive)}>
        <span class="ft-act-i">⌨</span>
        <span class="ft-act-l">{interactive ? 'typing' : 'keys off'}</span>
      </button>
      <button class="ft-act" class:on={menuOpen} onclick={() => (menuOpen = !menuOpen)}>
        <span class="ft-act-i">⋯</span><span class="ft-act-l">menu</span>
      </button>
    </nav>
  {/if}
</div>

<style>
  .fleet-term {
    position: fixed;
    inset: 0;
    z-index: 50;
    display: flex;
    flex-direction: column;
    background: #101010;
    color: var(--text, #d6d6d6);
  }
  .ft-head {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    background: var(--surface2, #181818);
    border-bottom: 1px solid var(--border, #2a2a2a);
  }
  .ft-back {
    font-size: 1.4rem;
    line-height: 1;
    background: none;
    border: none;
    color: var(--text, #ddd);
    padding: 0 6px;
    cursor: pointer;
  }
  .ft-title { display: flex; flex-direction: column; min-width: 0; flex: 1 1 auto; }
  .ft-name { font-weight: 600; font-size: 0.95rem; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .ft-sub { font-size: 0.72rem; color: var(--text3, #888); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .ft-status { flex: 0 0 auto; font-size: 0.72rem; color: var(--text3, #888);
    font-family: var(--mono, monospace); }
  .ft-status.live { color: var(--ok, #98c379); }
  .ft-status.err { color: var(--danger, #e06c75); }

  .ft-body { flex: 1 1 auto; min-height: 0; display: flex; }

  .ft-notattach { flex: 1 1 auto; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 8px; padding: 24px;
    text-align: center; color: var(--text3, #999); }
  .ft-notattach-sub { font-size: 0.82rem; max-width: 30ch; }

  .ft-grid {
    flex: 0 0 auto;
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 6px;
    padding: 8px;
    background: var(--surface2, #161616);
    border-top: 1px solid var(--border, #2a2a2a);
  }
  .ft-cmd {
    display: flex; flex-direction: column; align-items: center; gap: 2px;
    padding: 10px 4px;
    background: var(--surface3, #222);
    border: 1px solid var(--border, #333);
    border-radius: 8px;
    color: var(--text, #ddd);
    cursor: pointer;
    min-height: 48px;
  }
  .ft-cmd:active { background: var(--surface4, #2c2c2c); }
  .ft-cmd.danger { border-color: var(--danger, #e06c75); color: var(--danger, #e06c75); }
  .ft-cmd-label { font-weight: 600; font-size: 0.9rem; }
  .ft-cmd-hint { font-size: 0.62rem; color: var(--text3, #888); }

  .ft-compose {
    flex: 0 0 auto;
    display: flex;
    gap: 6px;
    padding: 6px 8px;
    background: var(--surface2, #161616);
    border-top: 1px solid var(--border, #2a2a2a);
  }
  .ft-input {
    flex: 1 1 auto; min-width: 0;
    padding: 10px 12px;
    background: var(--surface, #0d0d0d);
    border: 1px solid var(--border, #333);
    border-radius: 20px;
    color: var(--text, #eee);
    font-size: 16px; /* ≥16px avoids iOS zoom-on-focus */
  }
  .ft-send {
    flex: 0 0 auto;
    width: 44px; border-radius: 22px;
    background: var(--accent, #4b8bff);
    border: none; color: #fff; font-size: 1.1rem; cursor: pointer;
  }
  .ft-send:disabled { opacity: 0.4; }

  .ft-bar {
    flex: 0 0 auto;
    display: flex;
    padding: 4px 4px calc(4px + env(safe-area-inset-bottom, 0px));
    background: var(--surface2, #181818);
    border-top: 1px solid var(--border, #2a2a2a);
  }
  .ft-act {
    flex: 1 1 0;
    display: flex; flex-direction: column; align-items: center; gap: 2px;
    padding: 8px 2px;
    background: none; border: none; cursor: pointer;
    color: var(--text3, #999);
  }
  .ft-act.on { color: var(--accent, #4b8bff); }
  .ft-act-i { font-size: 1.15rem; line-height: 1; }
  .ft-act-l { font-size: 0.66rem; }
</style>
