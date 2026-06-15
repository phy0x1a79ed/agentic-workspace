/**
 * buildIndex unit tests — the true-DAG adjacency, with the shared-dependency
 * case a tree model can't represent.
 *
 * Runs under Node's built-in test runner with TypeScript type-stripping
 * (`node --test src/graph-index.test.ts`); graph-index.ts has only type-only
 * imports so it loads standalone.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import type { DagSnapshot, DagTask, DagContract, DagEdge } from '@awm/client';
import { buildIndex, upstream, downstream } from './graph-index.ts';

function task(id: string, extra: Partial<DagTask> = {}): DagTask {
  return {
    task_id: id, goal: id, state: 'blocked', is_root: false, mode: null,
    scope_slug: null, agent_ref: null, created_at: 0, updated_at: 0, ...extra,
  };
}
function contract(id: string, name: string, producer: string,
                  delivered = false): DagContract {
  return {
    contract_id: id, name, spec: '', producer_task: producer,
    delivered, payload_ref: delivered ? 'artifact:x' : null,
    delivered_ts: delivered ? 1 : null,
  };
}
function edge(id: string, consumer: string, c: DagContract): DagEdge {
  return {
    edge_id: id, consumer_task: consumer, contract_id: c.contract_id,
    contract_name: c.name, producer_task: c.producer_task,
    delivered: c.delivered,
  };
}

/** A produces `shared`, consumed by B, C and root — one producer, 3 consumers. */
function sharedDag(): DagSnapshot {
  const shared = contract('ct-shared', 'shared', 'A');
  const bOut = contract('ct-b', 'b_out', 'B');
  const cOut = contract('ct-c', 'c_out', 'C');
  return {
    project: 'p', root_id: 'root',
    tasks: [
      task('root', { is_root: true }), task('A', { state: 'active' }),
      task('B'), task('C'),
    ],
    contracts: [shared, bOut, cOut],
    edges: [
      edge('e1', 'B', shared), edge('e2', 'C', shared),
      edge('e3', 'root', shared),
      edge('e4', 'root', bOut), edge('e5', 'root', cOut),
    ],
  };
}

test('indexes tasks and contracts by id', () => {
  const ix = buildIndex(sharedDag());
  assert.equal(ix.taskById.get('A')?.state, 'active');
  assert.equal(ix.contractById.get('ct-shared')?.name, 'shared');
});

test('a shared producer feeds every consumer (true DAG, not a tree)', () => {
  const ix = buildIndex(sharedDag());
  const feeds = downstream(ix, 'A').map((n) => n.taskId).sort();
  assert.deepEqual(feeds, ['B', 'C', 'root'], 'A feeds all three consumers');
  // Each downstream hop is labelled by the connecting contract.
  assert.ok(downstream(ix, 'A').every((n) => n.contractName === 'shared'));
});

test('upstream lists the producers a task needs', () => {
  const ix = buildIndex(sharedDag());
  assert.deepEqual(upstream(ix, 'B').map((n) => n.taskId), ['A']);
  assert.equal(upstream(ix, 'B')[0].contractName, 'shared');
  // root needs all three top-level deliverables.
  assert.deepEqual(
    upstream(ix, 'root').map((n) => n.contractName).sort(),
    ['b_out', 'c_out', 'shared'],
  );
});

test('sources have no upstream; the root sink has no downstream', () => {
  const ix = buildIndex(sharedDag());
  assert.deepEqual(upstream(ix, 'A'), []);      // A is a source (produces only)
  assert.deepEqual(downstream(ix, 'root'), []); // root produces nothing
});

test('delivery flag rides each neighbour ref', () => {
  const snap = sharedDag();
  // Mark `shared` delivered on both the contract and its edges (as the page does).
  snap.contracts[0].delivered = true;
  for (const e of snap.edges) if (e.contract_name === 'shared') e.delivered = true;
  const ix = buildIndex(snap);
  assert.ok(downstream(ix, 'A').every((n) => n.delivered), 'shared edges delivered');
  assert.equal(upstream(ix, 'B')[0].delivered, true);
});
