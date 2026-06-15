/**
 * The orchestrator DAG read — one typed entry point shared by the dag-graph
 * component and the telemetry page. Mirrors the `fetchScope` / `subscribeAgent`
 * idiom in ./channel.ts: a thin typed wrapper over `svc('orchestrator').fn(...)`.
 *
 * The orchestrator's plan is a single global dependency DAG. `orch_dag` returns
 * the three tables in one shot — tasks (nodes), contracts (the unit of
 * hand-off), and the dependency edges denormalized to carry both endpoints +
 * the contract label, so a client builds adjacency without re-joining.
 */

import { svc } from './svc';

/** The explicit task lifecycle (see the orchestrator kernel's STATES). */
export type TaskState =
  | 'blocked'
  | 'ready'
  | 'planning'
  | 'plan_delivered'
  | 'verifying_plan'
  | 'plan_approved'
  | 'active'
  | 'decompose_pending'
  | 'decomposing'
  | 'completed'
  | 'failed'
  | 'abandoned';

export interface DagTask {
  task_id: string;
  goal: string;
  state: TaskState;
  is_root: boolean;
  /** 'plan' | 'verify' | 'worker' | 'planner' | null — the placement (if any) currently out. */
  mode: string | null;
  workspace_slug: string | null;
  agent_ref: string | null;
  /** Human-attached flag (orthogonal to lifecycle). */
  attached?: boolean;
  created_at: number;
  updated_at: number;
}

export interface DagContract {
  contract_id: string;
  name: string;
  spec: string;
  /** The single task that produces this contract. */
  producer_task: string;
  delivered: boolean;
  payload_ref: string | null;
  delivered_ts: number | null;
}

/**
 * A dependency edge: `consumer_task` needs the contract produced by
 * `producer_task`. Denormalized — `contract_name` is the edge label and
 * `delivered` tracks whether the consumed contract has been handed off.
 */
export interface DagEdge {
  edge_id: string;
  consumer_task: string;
  contract_id: string;
  contract_name: string;
  producer_task: string;
  delivered: boolean;
}

export interface DagSnapshot {
  project: string | null;
  /** The global root sentinel task id (a consumer of all top-level work). */
  root_id: string | null;
  tasks: DagTask[];
  contracts: DagContract[];
  edges: DagEdge[];
}

/** Fetch the whole plan in one call. Omit `project` for the global DAG. */
export function fetchDag(project?: string): Promise<DagSnapshot> {
  return svc('orchestrator').fn<DagSnapshot>(
    'orch_dag',
    project ? { project } : {},
  );
}
