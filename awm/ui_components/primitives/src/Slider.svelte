<script lang="ts">
  /**
   * Numeric slider with an inline readout. Continuous via the native
   * range input (cheap accessibility) but skinned to match the
   * underline/mono aesthetic of the rest of the primitives.
   *
   * Emits `onchange(n)` on every input event — the parent decides
   * whether to debounce. Step defaults to 1 for integers, 0.01 for
   * floats; callers pass `step` explicitly when they want finer/coarser.
   */
  interface Props {
    value: number;
    min: number;
    max: number;
    step?: number;
    disabled?: boolean;
    /** Decimals to show in the readout. Defaults to 0 if step is integer-like, else 2. */
    precision?: number;
    onchange?: (v: number) => void;
  }
  let {
    value,
    min,
    max,
    step = 1,
    disabled = false,
    precision,
    onchange,
  }: Props = $props();

  const fmtPrecision = $derived(precision ?? (Number.isInteger(step) ? 0 : 2));

  function fmt(n: number): string {
    if (!Number.isFinite(n)) return String(n);
    return n.toFixed(fmtPrecision);
  }

  function onInput(ev: Event) {
    const v = Number((ev.currentTarget as HTMLInputElement).value);
    onchange?.(v);
  }
</script>

<div class="slider" class:disabled data-awm-component="Slider">
  <input
    type="range"
    class="range"
    {min}
    {max}
    {step}
    {value}
    {disabled}
    oninput={onInput}
    aria-valuemin={min}
    aria-valuemax={max}
    aria-valuenow={value}
  />
  <span class="readout mono">{fmt(value)}</span>
</div>

<style>
  .slider {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: var(--space-3);
    align-items: center;
    width: 100%;
  }
  .slider.disabled { opacity: 0.55; }

  .readout {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--atomizer);
    min-width: 3ch;
    text-align: right;
    letter-spacing: 0.5px;
  }

  .range {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: 18px;
    background: transparent;
    margin: 0;
    cursor: pointer;
  }
  .range:disabled { cursor: default; }
  .range:focus { outline: none; }

  /* Track (webkit/blink) */
  .range::-webkit-slider-runnable-track {
    height: 2px;
    background: var(--border);
    border-radius: 1px;
  }
  .range:hover:not(:disabled)::-webkit-slider-runnable-track,
  .range:focus-visible::-webkit-slider-runnable-track {
    background: color-mix(in oklab, var(--atomizer) 50%, var(--border));
  }

  /* Track (firefox) */
  .range::-moz-range-track {
    height: 2px;
    background: var(--border);
    border-radius: 1px;
  }
  .range:hover:not(:disabled)::-moz-range-track,
  .range:focus-visible::-moz-range-track {
    background: color-mix(in oklab, var(--atomizer) 50%, var(--border));
  }

  /* Thumb (webkit/blink) */
  .range::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 12px;
    height: 12px;
    margin-top: -5px;
    border-radius: 2px;
    background: var(--surface3);
    border: 1px solid var(--atomizer);
    transition: background 120ms var(--ease-mech),
                transform 120ms var(--ease-mech);
  }
  .range:hover:not(:disabled)::-webkit-slider-thumb,
  .range:focus-visible::-webkit-slider-thumb {
    background: color-mix(in oklab, var(--atomizer) 35%, var(--surface3));
    transform: scale(1.08);
  }

  /* Thumb (firefox) */
  .range::-moz-range-thumb {
    width: 10px;
    height: 10px;
    border-radius: 2px;
    background: var(--surface3);
    border: 1px solid var(--atomizer);
    transition: background 120ms var(--ease-mech),
                transform 120ms var(--ease-mech);
  }
  .range:hover:not(:disabled)::-moz-range-thumb,
  .range:focus-visible::-moz-range-thumb {
    background: color-mix(in oklab, var(--atomizer) 35%, var(--surface3));
    transform: scale(1.08);
  }
</style>
