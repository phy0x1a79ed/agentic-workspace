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

/**
 * Result of opening a node — the orchestrator creates the task, places an
 * attended worker on it, and hands back the attach handle. `workspace_slug` is
 * the transcript/terminal key a chat then attaches to (the agent's identity);
 * `agent_ref` is the placement's slug.
 */
export interface OpenNodeResult {
  task_id: string;
  workspace_slug: string;
  agent_ref: string | null;
}

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
  /** Human-set headline, shown above the goal (separate from the goal text). */
  title: string;
  /** Free-text tags (searchable in the task list). Defaults to `[]`. */
  tags: string[];
  state: TaskState;
  is_root: boolean;
  /** 'plan' | 'verify' | 'worker' | 'planner' | null — the placement (if any) currently out. */
  mode: string | null;
  workspace_slug: string | null;
  agent_ref: string | null;
  /** Human-attached flag (orthogonal to lifecycle; tied to live chat presence). */
  attached?: boolean;
  /** Sticky pause: freezes the autonomous supervisor; survives chat disconnect. */
  paused?: boolean;
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
  /** The global root sentinel task id (a consumer of all top-level work). */
  root_id: string | null;
  tasks: DagTask[];
  contracts: DagContract[];
  edges: DagEdge[];
}

/** Fetch the whole (single global) plan in one call. */
export function fetchDag(): Promise<DagSnapshot> {
  return svc('orchestrator').fn<DagSnapshot>('orch_dag', {});
}

/**
 * Open a node at the root (omit `consumer` = a top-level prerequisite of the
 * global root). The orchestrator creates the task AND places an attended worker
 * on it, returning the `workspace_slug` the chat attaches to. This is the DAG
 * replacement for the old conversational "spawn an agent in a scope" — a public
 * op (no identity header needed).
 */
export function openNode(goal: string, repo?: unknown): Promise<OpenNodeResult> {
  return svc('orchestrator').fn<OpenNodeResult>('orch_node_open', {
    goal,
    ...(repo !== undefined ? { repo } : {}),
  });
}

/**
 * Set a task's human title (the headline above its goal). Public op — the user's
 * own edit, no identity header. The agent counterpart writes it via the agents
 * attach-gated admin relay, only while a human is connected.
 */
export function setTitle(
  taskId: string,
  title: string,
): Promise<{ ok?: boolean; title?: string }> {
  return svc('orchestrator').fn('orch_set_title', { task_id: taskId, title });
}

/** Set a task's free-text tags (a list of strings; searchable). Public op. */
export function setTags(
  taskId: string,
  tags: string[],
): Promise<{ ok?: boolean; tags?: string[] }> {
  return svc('orchestrator').fn('orch_set_tags', { task_id: taskId, tags });
}

/**
 * Toggle a task's sticky `paused` flag by its unit slug. Routes through the
 * agents service so the LIVE placement's supervisor freezes/resumes at once;
 * the agents side mirrors it durably to the orchestrator (passing `task_id` so
 * the durable write lands even when no placement is currently live). The flag
 * survives chat disconnect — that is the whole point (keep the supervisor out
 * while you walk away).
 */
export function setPaused(
  slug: string,
  paused: boolean,
  taskId?: string,
): Promise<{ ok?: boolean; paused?: boolean }> {
  return svc('agents').fn('set_paused', {
    scope: slug,
    paused,
    ...(taskId ? { task_id: taskId } : {}),
  });
}

/**
 * Result of creating a vague task — the orchestrator creates the task and rests
 * it in a planner-needed state; the planner is placed asynchronously and mints
 * the unit slug on dispatch, so NO `workspace_slug` comes back here (unlike
 * `openNode`). The caller selects by `task_id` and lets the DAG poll fill in the
 * slug a beat later.
 */
export interface CreateTaskResult {
  task_id: string;
  state: TaskState;
  consumer: string | null;
}

/**
 * Create a vague top-level task and let the orchestrator's PLANNER specify it
 * conversationally (the "plan it first" path), as opposed to `openNode` which
 * drops an attended worker straight onto a concrete goal. Public op (no identity
 * header). The task appears in a specifying state and may spawn child tasks.
 */
export function createTask(goal: string): Promise<CreateTaskResult> {
  return svc('orchestrator').fn<CreateTaskResult>('orch_task_create', { goal });
}

/**
 * Re-point the ATTACHED node's funnel onto another consumer (the global root by
 * default). Routes through the agents service's attach-gated admin relay, which
 * resolves the caller's placement from the `X-Awm-As: <slug>` header — so the
 * caller must pass the live unit slug it is attached to (not the default
 * operator identity). Rejected by the relay when no human is attached.
 */
export function relocateSelf(
  slug: string,
  newConsumerTask?: string,
): Promise<{ ok?: boolean; [k: string]: unknown }> {
  return svc('agents').request('/fn/relocate_self', {
    method: 'POST',
    headers: { 'X-Awm-As': slug },
    body: newConsumerTask ? { new_consumer_task: newConsumerTask } : {},
  });
}
