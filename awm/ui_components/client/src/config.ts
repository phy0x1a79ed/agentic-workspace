/**
 * Config contracts client — the settings page's two calls.
 *
 * `fetchConfigContracts` lists every opted-in service contract (+ the gateway
 * global slot) with its live values; `saveConfigContract` writes one contract's
 * values back to its owning service. Both are verbs of the gateway's native
 * `awm` tool over `POST /invoke`.
 */
import { tool } from './invoke';

export interface ConfigContract {
  /** Owner key: a service name, or 'gateway' for the global slot. */
  key: string;
  title: string;
  /** JSON-Schema of the settable fields (the DynamicConfigForm input). */
  schema: Record<string, unknown>;
  /** Live values; null when the owning service is registered but down. */
  values: Record<string, unknown> | null;
  /** True when the owner is unreachable — render the card disabled. */
  unavailable: boolean;
}

/** `awm config_contracts` — every published contract + live values. */
export function fetchConfigContracts(): Promise<{ contracts: ConfigContract[] }> {
  return tool('awm').call<{ contracts: ConfigContract[] }>('config_contracts');
}

/** `awm config_set` — persist one contract's values to its owner. */
export function saveConfigContract(
  key: string,
  values: Record<string, unknown>,
): Promise<ConfigContract> {
  return tool('awm').call<ConfigContract>('config_set', { key, values });
}
