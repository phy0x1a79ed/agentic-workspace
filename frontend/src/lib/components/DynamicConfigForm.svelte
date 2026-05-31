<script lang="ts">
  /**
   * Render a flat key/value form from a Pydantic JSON schema. Only one level
   * deep — nested objects/arrays render as a JSON textarea fallback. Enough
   * for the engine config knobs we expose today (paths, booleans, ints,
   * enums, refs); engines that need rich nesting can ship a custom form.
   */
  import Input from '$lib/primitives/Input.svelte';
  import Select from './Select.svelte';
  import PanelLabel from '$lib/primitives/PanelLabel.svelte';

  type JsonSchema = {
    type?: string;
    properties?: Record<string, FieldSchema>;
    required?: string[];
    $defs?: Record<string, FieldSchema>;
    [k: string]: unknown;
  };
  type FieldSchema = {
    type?: string;
    title?: string;
    description?: string;
    enum?: (string | number | boolean)[];
    default?: unknown;
    anyOf?: FieldSchema[];
    [k: string]: unknown;
  };

  interface Props {
    schema: JsonSchema;
    value?: Record<string, unknown>;
    onchange?: (value: Record<string, unknown>) => void;
    disabled?: boolean;
  }
  let { schema, value = $bindable({}), onchange, disabled = false }: Props = $props();

  const properties: [string, FieldSchema][] = $derived(
    Object.entries(schema?.properties ?? {})
  );

  function pickType(f: FieldSchema): string {
    if (f.type) return f.type;
    if (f.anyOf) {
      // Pydantic Optional[T] renders as anyOf [T, null]; pick the non-null.
      const nn = f.anyOf.find(a => a.type && a.type !== 'null');
      if (nn?.type) return nn.type;
    }
    return 'string';
  }

  function fieldEnum(f: FieldSchema): (string | number | boolean)[] | null {
    if (f.enum) return f.enum;
    if (f.anyOf) {
      const e = f.anyOf.find(a => a.enum);
      if (e?.enum) return e.enum;
    }
    return null;
  }

  function strValue(v: unknown): string {
    if (v === null || v === undefined) return '';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
  }

  function emit(next: Record<string, unknown>) {
    value = next;
    onchange?.(next);
  }

  function setText(key: string, raw: string, type: string) {
    const next = { ...value };
    if (raw === '') {
      delete next[key];
      emit(next);
      return;
    }
    if (type === 'integer' || type === 'number') {
      const n = Number(raw);
      if (Number.isFinite(n)) { next[key] = n; emit(next); return; }
      next[key] = raw;  // let backend coerce/reject
    } else {
      next[key] = raw;
    }
    emit(next);
  }

  function setBool(key: string, b: boolean) {
    const next = { ...value, [key]: b };
    emit(next);
  }

  function setJson(key: string, raw: string) {
    const next = { ...value };
    if (raw === '') { delete next[key]; emit(next); return; }
    try {
      next[key] = JSON.parse(raw);
    } catch {
      next[key] = raw;  // keep raw text; backend will fail validation if bad
    }
    emit(next);
  }
</script>

<div class="form">
  {#if properties.length === 0}
    <div class="empty">no configurable fields</div>
  {/if}
  {#each properties as [key, field] (key)}
    {@const t = pickType(field)}
    {@const e = fieldEnum(field)}
    <div class="row">
      <span class="k">
        <PanelLabel>{field.title || key}</PanelLabel>
      </span>
      <div class="v">
        {#if e}
          <Select
            value={strValue(value[key] ?? field.default)}
            options={e.map(String)}
            {disabled}
            onchange={(v) => setText(key, v, 'string')}
          />
        {:else if t === 'boolean'}
          <label class="bool">
            <input
              type="checkbox"
              checked={Boolean(value[key] ?? field.default)}
              {disabled}
              onchange={(ev) => setBool(key, (ev.currentTarget as HTMLInputElement).checked)}
            />
            <span class="bool-label">{Boolean(value[key] ?? field.default) ? 'on' : 'off'}</span>
          </label>
        {:else if t === 'object' || t === 'array'}
          <textarea
            class="ta mono"
            rows="2"
            placeholder={t === 'object' ? '{}' : '[]'}
            {disabled}
            value={strValue(value[key] ?? field.default ?? '')}
            oninput={(ev) => setJson(key, (ev.currentTarget as HTMLTextAreaElement).value)}
          ></textarea>
        {:else}
          <Input
            value={strValue(value[key] ?? field.default ?? '')}
            placeholder={field.description ?? ''}
            {disabled}
            oninput={(ev) => setText(key, (ev.currentTarget as HTMLInputElement).value, t)}
          />
        {/if}
        {#if field.description}
          <div class="hint">{field.description}</div>
        {/if}
      </div>
    </div>
  {/each}
</div>

<style>
  .form { display: flex; flex-direction: column; gap: var(--space-2); }
  .empty { color: var(--text3); font-size: 11px; font-style: italic; padding: var(--space-1) 0; }
  .row {
    display: grid;
    grid-template-columns: 100px 1fr;
    gap: var(--space-2);
    align-items: start;
  }
  .k { padding-top: 6px; text-align: right; }
  .v { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .hint { color: var(--text3); font-size: 10px; }
  .bool { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--text2); }
  .bool-label { font-family: var(--mono); }
  .ta {
    width: 100%;
    padding: 6px 8px;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    font-size: 11px;
    resize: vertical;
  }
  .ta:focus { outline: none; border-color: var(--atomizer); }
  .mono { font-family: var(--mono); }

  @media (max-width: 480px) {
    .row { grid-template-columns: 1fr; }
    .k { text-align: left; padding-top: 0; }
  }
</style>
