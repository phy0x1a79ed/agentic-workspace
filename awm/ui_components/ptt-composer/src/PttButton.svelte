<script lang="ts">
  // Visual state + spacebar binding for the mic-talk button. The composer
  // owns the actual audio-capture lifecycle and consumes onpttdown/onpttup
  // — this component is purely the input affordance.

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
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    color: var(--text2, #bbb);
    border-radius: 4px;
    padding: 12px;
    cursor: pointer;
    font-family: var(--mono, monospace);
    font-size: 12px;
    letter-spacing: 1px;
    text-transform: uppercase;
    min-height: 52px;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .ptt:hover  { background: var(--surface3, #2a2a2a); }
  .ptt.active {
    background: color-mix(in oklab, var(--recording, #f55) 30%, var(--surface2, #222));
    color: var(--text, #ddd);
    border-color: var(--recording, #f55);
  }
  .touch-hint { display: none; }
  @media (hover: none), (max-width: 720px) {
    .ptt { min-height: 60px; font-size: 13px; }
    .kbd-hint   { display: none; }
    .touch-hint { display: inline; }
  }
</style>
