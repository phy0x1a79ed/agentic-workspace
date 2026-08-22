/**
 * Pure adjacency index over a DAG snapshot — the only non-trivial logic in the
 * component, kept free of Svelte so it unit-tests standalone under Node's test
 * runner (type-only imports; see graph-index.test.ts).
 *
 * The orchestrator plan is a TRUE DAG, not a tree: a contract produced by one
 * task can be consumed by many tasks, so a node may have multiple downstream
 * consumers. We therefore build two flat adjacency maps (no nesting) keyed by
 * task id, and the selected-task focus panel reads its immediate neighbours off
 * them. A shared node legitimately appears under several selections — every copy
 * is read from this one index, so they stay in sync by construction.
 */

import type { DagSnapshot, DagTask, DagContract } from '@awm/client';

/** One hop along a dependency edge, labelled by the connecting contract. */
export interface NeighborRef {
  /** The task at the other end of the edge. */
  taskId: string;
  contractId: string;
  /** The edge label — the contract that connects the two tasks. */
  contractName: string;
  /** Whether the connecting contract has been delivered (handed off). */
  delivered: boolean;
}

export interface DagIndex {
  taskById: Map<string, DagTask>;
  contractById: Map<string, DagContract>;
  /** task → the producers it depends on ("needs" / upstream / prerequisites). */
  upstreamOf: Map<string, NeighborRef[]>;
  /** task → the consumers it feeds ("what's next" / downstream / dependents). */
  downstreamOf: Map<string, NeighborRef[]>;
}

function push(map: Map<string, NeighborRef[]>, key: string, ref: NeighborRef) {
  const list = map.get(key);
  if (list) list.push(ref);
  else map.set(key, [ref]);
}

/**
 * Build the index from a snapshot. O(edges); rebuilt reactively whenever the
 * snapshot reference changes (a live state patch hands a new object), so it
 * never needs to be mutated in place.
 */
export function buildIndex(snapshot: DagSnapshot): DagIndex {
  const taskById = new Map<string, DagTask>();
  for (const t of snapshot.tasks) taskById.set(t.task_id, t);

  const contractById = new Map<string, DagContract>();
  for (const c of snapshot.contracts) contractById.set(c.contract_id, c);

  const upstreamOf = new Map<string, NeighborRef[]>();
  const downstreamOf = new Map<string, NeighborRef[]>();

  for (const e of snapshot.edges) {
    // consumer NEEDS producer (upstream of the consumer).
    push(upstreamOf, e.consumer_task, {
      taskId: e.producer_task,
      contractId: e.contract_id,
      contractName: e.contract_name,
      delivered: e.delivered,
    });
    // producer FEEDS consumer (downstream of the producer).
    push(downstreamOf, e.producer_task, {
      taskId: e.consumer_task,
      contractId: e.contract_id,
      contractName: e.contract_name,
      delivered: e.delivered,
    });
  }

  return { taskById, contractById, upstreamOf, downstreamOf };
}

/** Immediate dependencies of a task (empty array if none / unknown). */
export function upstream(index: DagIndex, taskId: string): NeighborRef[] {
  return index.upstreamOf.get(taskId) ?? [];
}

/** Immediate dependents of a task (empty array if none / unknown). */
export function downstream(index: DagIndex, taskId: string): NeighborRef[] {
  return index.downstreamOf.get(taskId) ?? [];
}
