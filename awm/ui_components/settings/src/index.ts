// @awm/settings — the reusable config-settings surface.
//
// SettingsPanel renders one card per published config contract (fetched from the
// gateway aggregator); DynamicConfigForm is the JSON-Schema → form engine it
// uses, also exported on its own for pages that render a single schema (the TTS
// engine config card).
export { default as SettingsPanel } from './SettingsPanel.svelte';
export { default as ContractCard } from './ContractCard.svelte';
export { default as DynamicConfigForm } from './DynamicConfigForm.svelte';
