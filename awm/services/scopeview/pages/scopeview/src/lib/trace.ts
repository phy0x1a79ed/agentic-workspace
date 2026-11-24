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

/**
 * The row-key values in a mark's customdata. A scalar is the key. A list of one type is the keys of an
 * aggregated mark. A mixed list is a Plotly Express hover row, whose first column is the key.
 */
function keyValues(customdata: unknown): string[] {
  const list = Array.isArray(customdata) ? customdata : [customdata];
  const uniform = list.every((v) => typeof v === typeof list[0]);
  return (uniform ? list : list.slice(0, 1))
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
