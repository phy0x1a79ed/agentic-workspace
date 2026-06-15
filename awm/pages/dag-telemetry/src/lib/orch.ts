/**
 * Orchestrator status client for the telemetry page.
 *
 * `orch_status` already returns everything the task list needs (the task rows
 * with state + the workspace_slug that IS the transcript key, plus escalations),
 * so the list view needs no new backend op. svc-orchestrator's `@awm/dag-graph`
 * flowchart can later replace the list behind the same `onSelectTask` event,
 * fed from the same rows.
 */
import { svc } from '@awm/client';

/** One task row from `orch_status` (see operations.orch_status). */
export interface OrchTask {
  task_id: string;
  goal: string;
  state: string;
  is_root: boolean;
  /** The workspace unit slug — also the agents transcript key for this task. */
  workspace_slug: string | null;
  agent_ref: string | null;
  mode: string | null;
  attached: boolean;
}

export interface OrchEscalation {
  task_id: string;
  goal: string;
  state: string;
}

export interface OrchStatus {
  project: string | null;
  counts: Record<string, number>;
  complete: boolean;
  escalations: OrchEscalation[];
  tasks: OrchTask[];
}

/** Fetch the current orchestrator status (optionally scoped to a project). */
export async function fetchStatus(project?: string): Promise<OrchStatus> {
  return svc('orchestrator').fn<OrchStatus>(
    'orch_status',
    project ? { project } : {},
  );
}
