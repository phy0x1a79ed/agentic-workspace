import type { ComponentProps } from 'svelte';
import Component from './DynamicConfigForm.svelte';

// One variant per JSON Schema shape DynamicConfigForm must eventually
// handle. Today the form renders every non-boolean/non-enum/non-object
// field as a plain text input — variants pass crash-on-mount because the
// schema-blind code path is uniform. When widget extension lands (separate
// scope), per-variant assertions get added that differentiate text-input
// vs. checkbox vs. slider vs. url input. The fixture set stays; only the
// test expectations evolve.
//
// JSON Schema blobs are hand-shaped (not codegen'd) — engine CONFIG_SCHEMA
// schemas escape the OpenAPI surface (see infra-typed-seams disclaimer).

const variant = (name: string, prop: Record<string, unknown>): ComponentProps<typeof Component> => ({
  schema: { type: 'object', properties: { [name]: prop } },
  value: {},
});

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  string:          variant('host', { type: 'string', title: 'Host' }),
  integerBounded:  variant('port', { type: 'integer', minimum: 0, maximum: 65535, title: 'Port' }),
  number:          variant('ratio', { type: 'number', title: 'Ratio' }),
  boolean:         variant('enabled', { type: 'boolean', title: 'Enabled', default: true }),
  enumString:      variant('mode', { type: 'string', enum: ['fast', 'balanced', 'quality'], default: 'balanced' }),
  uri:             variant('endpoint', { type: 'string', format: 'uri', title: 'Endpoint' }),
  optionalString:  variant('label', { anyOf: [{ type: 'string' }, { type: 'null' }], title: 'Label' }),
  empty:           { schema: { type: 'object', properties: {} }, value: {} },
};

export { Component as component };
export default fixtures;
