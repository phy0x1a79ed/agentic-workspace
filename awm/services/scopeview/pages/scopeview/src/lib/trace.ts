/** What the data panel points at when trace is on, and how a figure mark becomes a citation. */
import type { GraphNode, Trace } from './api';

export interface Target {
  /** Marks the element in the data panel; unique within one node's content. */
  key: string;
  /** What to call it in the info panel. */
  label: string;
  /** Citations for the `trace` verb to follow. */
  cites?: string[];
  /** A trace the page already holds, such as a table row's own values. */
  local?: Trace;
}

/** A mark's row keys: customdata[0] is the key, or the list of keys of an aggregated mark. Later columns are hover text. */
function keyValues(customdata: unknown): string[] {
  const first = Array.isArray(customdata) ? customdata[0] : customdata;
  return (Array.isArray(first) ? first : [first])
    .filter((v) => typeof v === 'string' || typeof v === 'number')
    .map(String)
    .filter((v) => !/[,&]/.test(v));
}

export function markTarget(figure: GraphNode, customdata: unknown): Target {
  const values = figure.rows ? keyValues(customdata) : [];
  if (!values.length) return { key: 'mark', label: figure.title, cites: [figure.id] };
  return {
    key: `mark:${values.join(',')}`,
    label: values.length > 1 ? `${values.length} ${figure.rows!.key} values` : values[0],
    cites: [`${figure.id}?${figure.rows!.key}=${values.join(',')}`],
  };
}
