<script lang="ts">
  // V1: visual state + spacebar binding only. The legacy voice-panel.js
  // (audio capture, WS to /voice/ws, STT auto-send) is a 276-line DOM-driven
  // IIFE — it's deferred to a follow-up that componentizes it properly.
  // The mic-worklet.js audio module is still shipped (frontend/static/) so
  // the eventual port doesn't need to re-add it.

  interface Props {
    disabled?: boolean;
    onpttdown?: () => void;
    onpttup?: () => void;
  }
  let { disabled = false, onpttdown, onpttup }: Props = $props();

  let active = $state(false);

  function down(e: Event) {
    if (disabled) return;
    e.preventDefault();
    if (active) return;
    active = true;
    onpttdown?.();
  }
  function up(e: Event) {
    if (!active) return;
    e.preventDefault();
    active = false;
    onpttup?.();
  }

  // Spacebar binding on desktop, when nothing else is focused.
  $effect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.code !== 'Space' || e.repeat) return;
      const t = e.target as HTMLElement;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      down(e);
    }
    function onKeyUp(e: KeyboardEvent) {
      if (e.code !== 'Space') return;
      up(e);
    }
    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
    };
  });
</script>

<button
  class="ptt"
  class:active
  type="button"
  {disabled}
  aria-pressed={active}
  onmousedown={down}
  onmouseup={up}
  onmouseleave={up}
  ontouchstart={down}
  ontouchend={up}
  ontouchcancel={up}
>
  <span class="kbd-hint">Hold <kbd>SPACE</kbd> or this button to talk</span>
  <span class="touch-hint">Hold to talk</span>
</button>

<style>
  .ptt {
    width: 100%;
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text2);
    border-radius: 4px;
    padding: 12px;
    cursor: pointer;
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: 1px;
    text-transform: uppercase;
    min-height: 52px;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .ptt:hover  { background: var(--surface3); }
  .ptt.active {
    background: color-mix(in oklab, var(--recording) 30%, var(--surface2));
    color: var(--text);
    border-color: var(--recording);
  }
  .touch-hint { display: none; }
  @media (hover: none), (max-width: 720px) {
    .ptt { min-height: 60px; font-size: 13px; }
    .kbd-hint   { display: none; }
    .touch-hint { display: inline; }
  }
</style>
