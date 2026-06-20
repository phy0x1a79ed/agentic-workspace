/**
 * View config for the dag-graph component — re-exports the wire types from
 * @awm/client and maps each task state to a display label, a primitives `Tag`
 * tone, and a list-group order (runnable/in-flight surfaced first, done last).
 */
import type { TaskState } from '@awm/client';

export type {
  TaskState, DagTask, DagContract, DagEdge, DagSnapshot,
} from '@awm/client';

export type TagTone = 'neutral' | 'ok' | 'warn' | 'danger' | 'atomizer' | 'mgr';

export interface StateMeta {
  label: string;
  tone: TagTone;
}

export const STATE_META: Record<TaskState, StateMeta> = {
  ready:             { label: 'ready',          tone: 'atomizer' },
  planning:          { label: 'planning',       tone: 'mgr' },
  plan_delivered:    { label: 'plan ready',     tone: 'atomizer' },
  verifying_plan:    { label: 'verifying',      tone: 'mgr' },
  plan_approved:     { label: 'plan ok',        tone: 'atomizer' },
  active:            { label: 'active',          tone: 'mgr' },
  decompose_pending: { label: 'decompose',      tone: 'warn' },
  decomposing:       { label: 'decomposing',    tone: 'warn' },
  blocked:           { label: 'blocked',         tone: 'neutral' },
  failed:            { label: 'failed',          tone: 'danger' },
  abandoned:         { label: 'abandoned',       tone: 'danger' },
  completed:         { label: 'completed',       tone: 'ok' },
};

/** Group order for the task list: what's runnable/in-flight first, done last. */
export const STATE_ORDER: TaskState[] = [
  'ready', 'planning', 'plan_delivered', 'verifying_plan', 'plan_approved',
  'active', 'decompose_pending', 'decomposing', 'blocked',
  'failed', 'abandoned', 'completed',
];
