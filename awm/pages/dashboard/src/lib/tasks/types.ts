// Task model for the dashboard board. NOTHING in the awm backend implements
// this yet (no tasks table/endpoints) — these types describe the shape the
// board renders against today (mock fixtures) and that a future backend will
// serve. See CONTRACT.md for the endpoint surface other scopes will build.

export type TaskStatus = 'queued' | 'running' | 'blocked' | 'error' | 'completed';

export const TASK_LANES: TaskStatus[] = [
  'queued',
  'running',
  'blocked',
  'error',
  'completed'
];

export interface TaskResult {
  success: boolean;
  summary: string;
}

export interface Task {
  id: string;
  /** project name — matches the `name` from GET /projects/search */
  projectId: string;
  title: string;
  acceptanceCriteria: string[];
  status: TaskStatus;
  /** "project/scope" → resolves to an agent room via ensureRoom() later. */
  assignedScope: string | null;
  /** task ids this one waits on; unmet ⇒ status 'blocked'. */
  dependsOn: string[];
  /** set once the task reaches completed/error. */
  result: TaskResult | null;
  /**
   * true ⇒ surfaces under "needs your input": either the orchestrator is
   * blocked on a human, or a completed task is awaiting review. The board
   * distinguishes a needs-human card from a quietly-done one.
   */
  needsHuman: boolean;
  createdAt: string; // ISO
  updatedAt: string; // ISO
}

export type ScopeCounts = { active: number; completed: number; deleted: number };

export interface ProjectAttention {
  /** project name (matches /projects/search). */
  name: string;
  scopeCounts: ScopeCounts;
  /** 0–100, higher = more in need of attention. See attention.ts. */
  attentionScore: number;
  /** per-lane task counts; absent until a tasks backend exists. */
  taskBreakdown?: Record<TaskStatus, number>;
}
