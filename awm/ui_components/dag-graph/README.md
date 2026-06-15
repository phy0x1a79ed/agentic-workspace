# @awm/dag-graph

A compact, **list-first** view of the orchestrator's task DAG. Deliberately
*not* a node-link canvas — that is heavy and space-hungry (the same reason
Airflow demoted its graph view in favour of a list). Instead:

- **TaskList** — every task grouped by state, runnable/in-flight surfaced first,
  done collapsed at the bottom. The global sense of the plan.
- **FocusPanel** — for the *selected* task, its immediate **dependencies**
  (upstream / what it *needs*) and **dependents** (downstream / *what's next*),
  each row labelled by the connecting contract + a delivered/pending mark.
  Clicking a neighbour re-selects it, so you walk the DAG one hop at a time —
  no canvas, no layout engine.

The plan is a **true DAG**, not a tree: a contract produced by one task can be
consumed by many, so a node can have several downstream consumers. A shared node
therefore appears under more than one selection — every copy is read from one
in-memory index keyed by `task_id`, so they stay in sync by construction.

## Usage (presentational)

The component owns nothing but indexing + rendering. The **page** owns the
fetch, the selection state, and live patching:

```svelte
<script lang="ts">
  import { DagGraph } from '@awm/dag-graph';
  import { fetchDag, type DagSnapshot } from '@awm/client';

  let snapshot = $state<DagSnapshot | null>(null);
  let selected = $state<string | null>(null);

  $effect(() => { fetchDag(/* project? */).then((s) => (snapshot = s)); });
</script>

{#if snapshot}
  <DagGraph
    {snapshot}
    selectedTaskId={selected}
    onSelectTask={(id) => (selected = id)}
  />
{/if}
```

The host page must `import '@awm/primitives/style.css'` for the design tokens.

### Props

| prop | type | meaning |
|---|---|---|
| `snapshot` | `DagSnapshot` | the whole plan (`fetchDag()` → `{tasks, contracts, edges, root_id}`) |
| `selectedTaskId` | `string \| null` | controlled selection (the page owns it) |
| `onSelectTask` | `(taskId: string) => void` | emitted on any row / neighbour click |

The only event is `onSelectTask`. The component never fetches and never opens a
socket.

## Live updates — the page-side patch contract

`DagGraph` re-derives its index whenever the `snapshot` **reference** changes, so
live updates are purely a matter of the page handing it a fresh object. From the
telemetry stream (`dag-dev`'s `awm.telemetry`, `task_state_changed`-style
events):

- **state change** → look the task up by `task_id`, set its `state`/`updated_at`,
  hand `DagGraph` a new `{ ...snapshot }`.
- **contract delivered** → flip the matching `contracts[].delivered` **and** the
  denormalized `edges[].delivered` for that contract id, then a new ref.
- **structural change** (`decompose_commit` / `orch_task_attach` add tasks,
  contracts, edges) is rare → just `fetchDag()` again. The stream carries *state*,
  not new topology.

De-dup is last-writer-wins on the id-keyed task map (the same shape as the
agent-transcript dedupe-by-`id`). No full refetch on a state tick.

## Static preview fixture

For a gallery / Storybook-style preview with no backend, feed this snapshot
directly (one producer, two consumers — exercises the shared-dependency case):

```ts
import type { DagSnapshot } from '@awm/client';

export const demoSnapshot: DagSnapshot = {
  project: 'demo', root_id: 'root',
  tasks: [
    { task_id: 'root', goal: 'ship the feature', state: 'blocked', is_root: true,
      mode: null, scope_slug: null, agent_ref: null, created_at: 0, updated_at: 0 },
    { task_id: 'A', goal: 'design the schema', state: 'completed', is_root: false,
      mode: null, scope_slug: 'demo-a', agent_ref: 'agent:demo-a', created_at: 1, updated_at: 5 },
    { task_id: 'B', goal: 'build the API', state: 'active', is_root: false,
      mode: 'worker', scope_slug: 'demo-b', agent_ref: 'agent:demo-b', created_at: 2, updated_at: 6 },
    { task_id: 'C', goal: 'write the docs', state: 'ready', is_root: false,
      mode: null, scope_slug: null, agent_ref: null, created_at: 3, updated_at: 3 },
  ],
  contracts: [
    { contract_id: 'ct-s', name: 'schema', spec: 'the DB schema', producer_task: 'A',
      delivered: true, payload_ref: 'artifact:schema', delivered_ts: 5 },
    { contract_id: 'ct-b', name: 'api', spec: '', producer_task: 'B',
      delivered: false, payload_ref: null, delivered_ts: null },
    { contract_id: 'ct-c', name: 'docs', spec: '', producer_task: 'C',
      delivered: false, payload_ref: null, delivered_ts: null },
  ],
  edges: [
    { edge_id: 'e1', consumer_task: 'B', contract_id: 'ct-s', contract_name: 'schema', producer_task: 'A', delivered: true },
    { edge_id: 'e2', consumer_task: 'C', contract_id: 'ct-s', contract_name: 'schema', producer_task: 'A', delivered: true },
    { edge_id: 'e3', consumer_task: 'root', contract_id: 'ct-b', contract_name: 'api', producer_task: 'B', delivered: false },
    { edge_id: 'e4', consumer_task: 'root', contract_id: 'ct-c', contract_name: 'docs', producer_task: 'C', delivered: false },
  ],
};
```

Mount `<DagGraph snapshot={demoSnapshot} {selected} onSelectTask={...} />`; select
**A** to see it feed both *build the API* and *write the docs* via `schema`.
