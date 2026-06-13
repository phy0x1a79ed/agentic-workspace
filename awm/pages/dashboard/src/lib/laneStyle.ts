import type { TaskStatus } from './tasks/types';

// Per-lane presentation: the token color of its accent + the Tag tone to use.
// The three "demands attention" states (error/blocked + awaiting-review) carry
// the loud colors; queued/running stay quiet.

export type TagTone = 'neutral' | 'ok' | 'warn' | 'danger' | 'atomizer' | 'mgr';

export const LANE_LABEL: Record<TaskStatus, string> = {
  queued: 'queued',
  running: 'running',
  blocked: 'blocked',
  error: 'error',
  completed: 'completed'
};

export const LANE_VAR: Record<TaskStatus, string> = {
  queued: 'var(--text3)',
  running: 'var(--atomizer)',
  blocked: 'var(--warn)',
  error: 'var(--danger)',
  completed: 'var(--ok)'
};

export const LANE_TONE: Record<TaskStatus, TagTone> = {
  queued: 'neutral',
  running: 'atomizer',
  blocked: 'warn',
  error: 'danger',
  completed: 'ok'
};

/** A completed-but-unreviewed task gets the manager accent, distinct from done. */
export const REVIEW_VAR = 'var(--mgr)';
