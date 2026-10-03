<script lang="ts">
  // Remote mic — one control, one meter, one status line. The transport lives in
  // ./lib/micStream; this component is the state machine's face.
  //
  // The badge, the ring and the button are coloured together by selectors that
  // hang off the single `data-state` attribute on <main>. That works because all
  // three live in this component; extracting the button into a shared primitive
  // would break the colouring silently, which is one reason this page keeps its
  // own markup instead of adopting the button primitive.
  import { onMount } from 'svelte';
  import { createMicStream, type MicState } from './lib/micStream';

  const RING = 2 * Math.PI * 52; // the ring's circumference, r=52

  let conn = $state<MicState>('connected');
  let message = $state('idle');
  let isError = $state(false);
  let streaming = $state(false);
  let dashOffset = $state(RING);

  // Meter smoothing runs outside the reactive graph: `peak` is written per audio
  // chunk (50 Hz) and read per animation frame, and neither should schedule a
  // Svelte update. Only `dashOffset` is $state.
  let peak = 0;
  let shown = 0;

  const insecure = typeof window !== 'undefined' && !window.isSecureContext;

  const mic = createMicStream({
    onState: (s) => { conn = s; streaming = s !== 'connected'; },
    onMessage: (text, err) => { message = text; isError = !!err; },
    onPeak: (p) => { peak = p; },
  });

  // The button label follows *intent* (did you press it), not the connection
  // state — so a reconnect in progress still reads STOP rather than flipping
  // back to START under the user.
  const label = $derived(streaming ? 'STOP' : 'START');
  const sub = $derived(streaming ? 'streaming — tap to stop' : 'tap to stream mic');

  function toggle() {
    if (mic.active) mic.stop();
    else void mic.start();
  }

  // Spacebar toggles. Three guards, all load-bearing: ignore auto-repeat while
  // the key is held, ignore it when the button itself is focused (it already
  // fires its own click), and preventDefault so the page doesn't scroll.
  function onKeydown(e: KeyboardEvent) {
    if (e.code !== 'Space' && e.key !== ' ') return;
    if (e.repeat) return;
    if (e.target instanceof HTMLElement && e.target.tagName === 'BUTTON') return;
    e.preventDefault();
    toggle();
  }

  onMount(() => {
    let raf = 0;
    const tick = () => {
      const target = mic.active ? peak : peak * 0.5;
      // Asymmetric smoothing: snap up on a transient, fall away slowly, so the
      // ring reads as a level meter rather than as noise.
      shown += (target - shown) * (target > shown ? 0.5 : 0.12);
      dashOffset = RING * (1 - Math.min(1, shown * 1.6));
      peak *= 0.9;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => { cancelAnimationFrame(raf); mic.destroy(); };
  });
</script>

<svelte:window on:keydown={onKeydown} />

<div class="bar">
  <div class="brand">AWM · <b>REMOTE MIC</b></div>
  <div class="conn" data-state={conn}>
    <span class="dot"></span><span class="txt">{conn}</span>
  </div>
</div>

{#if insecure}
  <p class="warnbar">
    ⚠ Not a secure context — the browser will block the mic. Open this page over
    https:// (the awm edge) or on http://localhost.
  </p>
{/if}

<main class="stage" data-state={conn}>
  <div class="meter">
    <svg class="ring" viewBox="0 0 120 120" aria-hidden="true">
      <circle class="ring-track" cx="60" cy="60" r="52"></circle>
      <circle
        class="ring-level" cx="60" cy="60" r="52"
        stroke-dasharray={RING.toFixed(1)}
        stroke-dashoffset={dashOffset.toFixed(1)}
      ></circle>
    </svg>
  </div>

  <button class="toggle" aria-pressed={streaming} onclick={toggle}>
    <span class="lab">{label}</span>
    <span class="sub">{sub}</span>
  </button>

  <p class="msg" class:err={isError}>{message}</p>

  <p class="foot">phone mic → this machine · <a href="/ca.crt">install certificate</a></p>
</main>

<style>
  .bar {
    flex: 0 0 auto;
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: max(10px, env(safe-area-inset-top)) 16px 10px;
    border-bottom: 1px solid var(--mic-line);
    background: linear-gradient(180deg, var(--mic-panel), var(--mic-panel2));
  }
  .brand {
    font-family: var(--mic-mono); font-size: .68rem; letter-spacing: .22em;
    color: var(--mic-muted); text-transform: uppercase;
  }
  .brand b { color: var(--mic-cyan); font-weight: 700; }

  .conn {
    display: flex; align-items: center; gap: 8px;
    font-family: var(--mic-mono); font-size: .66rem; letter-spacing: .12em;
    text-transform: uppercase;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--mic-muted); flex: 0 0 auto; }
  .conn[data-state="offline"] .dot { background: var(--mic-air); box-shadow: 0 0 9px var(--mic-air); }
  .conn[data-state="offline"] .txt { color: var(--mic-air); }
  .conn[data-state="connecting"] .dot { background: var(--mic-amber); animation: pulse 1s ease-in-out infinite; }
  .conn[data-state="connecting"] .txt { color: var(--mic-amber); }
  .conn[data-state="connected"] .dot { background: var(--mic-good); box-shadow: 0 0 9px var(--mic-good); }
  .conn[data-state="connected"] .txt { color: var(--mic-good); }
  .conn[data-state="streaming"] .dot { background: var(--mic-purple); box-shadow: 0 0 9px var(--mic-purple); animation: pulse 1.2s ease-in-out infinite; }
  .conn[data-state="streaming"] .txt { color: var(--mic-purple); }

  .warnbar {
    flex: 0 0 auto; margin: 0; padding: 10px 14px; font-size: .72rem; text-align: center;
    background: rgba(255, 176, 32, .10); border-top: 1px solid rgba(255, 176, 32, .35);
    color: var(--mic-amber);
  }

  .stage {
    flex: 1 1 auto;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 26px; padding: 24px 18px max(24px, env(safe-area-inset-bottom));
  }

  .meter { position: relative; width: 120px; height: 120px; flex: 0 0 auto; }
  .ring { position: absolute; inset: 0; transform: rotate(-90deg); overflow: visible; }
  .ring-track { fill: none; stroke: rgba(122, 164, 206, .14); stroke-width: 6; }
  .ring-level {
    fill: none; stroke: var(--mic-air); stroke-width: 8; stroke-linecap: round;
    transition: stroke .25s ease, filter .25s ease;
  }
  .stage[data-state="connected"] .ring-level { stroke: var(--mic-good); filter: drop-shadow(0 0 7px rgba(70, 230, 163, .6)); }
  .stage[data-state="streaming"] .ring-level { stroke: var(--mic-purple); filter: drop-shadow(0 0 9px rgba(160, 107, 255, .7)); }

  .toggle {
    width: 200px; min-height: 64px; border-radius: 16px; cursor: pointer;
    border: 1px solid var(--mic-line2); color: var(--mic-text);
    background:
      radial-gradient(120% 140% at 50% 20%, rgba(47, 217, 245, .10), transparent 60%),
      linear-gradient(180deg, #101a27, #0a121c);
    box-shadow: 0 10px 30px -16px rgba(0, 0, 0, .9), 0 1px 0 rgba(255, 255, 255, .05) inset;
    display: grid; place-content: center; gap: 3px; text-align: center;
    font-family: var(--mic-mono); user-select: none; -webkit-user-select: none;
    transition: transform .08s ease, box-shadow .2s, border-color .2s, background .2s;
  }
  .toggle:active { transform: scale(.985); }
  .toggle .lab { font-weight: 700; font-size: 1.25rem; letter-spacing: .14em; }
  .toggle .sub { font-size: .62rem; letter-spacing: .2em; text-transform: uppercase; color: var(--mic-muted); }

  .stage[data-state="connected"] .toggle {
    border-color: rgba(70, 230, 163, .6); color: #fff;
    background:
      radial-gradient(120% 140% at 50% 20%, rgba(70, 230, 163, .22), transparent 62%),
      linear-gradient(180deg, #0c1a15, #0a140f);
    box-shadow: 0 0 0 1px rgba(70, 230, 163, .35), 0 0 30px -8px rgba(70, 230, 163, .45), 0 10px 30px -16px rgba(0, 0, 0, .9);
  }
  .stage[data-state="streaming"] .toggle {
    border-color: rgba(160, 107, 255, .6); color: #fff;
    background:
      radial-gradient(120% 140% at 50% 20%, rgba(160, 107, 255, .26), transparent 62%),
      linear-gradient(180deg, #15102a, #0c0a18);
    box-shadow: 0 0 0 1px rgba(160, 107, 255, .4), 0 0 34px -6px rgba(160, 107, 255, .5), 0 10px 30px -16px rgba(0, 0, 0, .9);
    animation: streampulse 1.4s ease-in-out infinite;
  }
  .stage[data-state="offline"] .toggle {
    border-color: rgba(255, 59, 78, .55); color: #fff;
    background:
      radial-gradient(120% 140% at 50% 20%, rgba(255, 59, 78, .20), transparent 62%),
      linear-gradient(180deg, #1a1013, #130b0e);
    box-shadow: 0 0 0 1px rgba(255, 59, 78, .35), 0 10px 30px -16px rgba(0, 0, 0, .9);
  }
  .stage[data-state="connected"] .toggle .sub,
  .stage[data-state="streaming"] .toggle .sub,
  .stage[data-state="offline"] .toggle .sub { color: rgba(255, 255, 255, .72); }

  .msg {
    font-family: var(--mic-mono); font-size: .66rem; letter-spacing: .06em;
    color: var(--mic-cyan); min-height: 1em; text-align: center; margin: 0;
  }
  .msg.err { color: var(--mic-air); }

  .foot {
    font-family: var(--mic-mono); font-size: .6rem; letter-spacing: .08em;
    color: var(--mic-muted); text-align: center; margin: 0;
  }
  .foot a { color: var(--mic-muted); }

  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
  @keyframes streampulse {
    0%, 100% { box-shadow: 0 0 0 1px rgba(160, 107, 255, .4), 0 0 30px -8px rgba(160, 107, 255, .45), 0 10px 30px -16px rgba(0, 0, 0, .9); }
    50%      { box-shadow: 0 0 0 1px rgba(160, 107, 255, .6), 0 0 48px -2px rgba(160, 107, 255, .65), 0 10px 30px -16px rgba(0, 0, 0, .9); }
  }
</style>
