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
  ready:       { label: 'ready',       tone: 'atomizer' },
  active:      { label: 'active',      tone: 'mgr' },
  decomposing: { label: 'decomposing', tone: 'warn' },
  blocked:     { label: 'blocked',     tone: 'neutral' },
  failed:      { label: 'failed',      tone: 'danger' },
  abandoned:   { label: 'abandoned',   tone: 'danger' },
  completed:   { label: 'completed',   tone: 'ok' },
};

/** Group order for the task list: what's runnable/in-flight first, done last. */
export const STATE_ORDER: TaskState[] = [
  'ready', 'active', 'decomposing', 'blocked', 'failed', 'abandoned', 'completed',
];
