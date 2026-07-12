/**
 * `deriveAttention` — the attention strip's data derivation, pure and testable.
 *
 * Runs off the SAME 3s `orch_dag` poll the rest of the page uses — no second
 * feed. It surfaces EXACTLY the two things that genuinely need a human, each
 * entry carrying its age so the strip can foreground the stalest:
 *
 *   wants — a node waiting on the human: it flagged `steer_requested` mid-run,
 *           OR it is `attached` (a steering session is open / frozen on you).
 *   broken — a node that failed or was abandoned.
 *
 * No other categories (blocked/decomposing/etc. are autonomous, not attention).
 * The global root sentinel is bookkeeping, never surfaced. Ages come from
 * `updated_at`, which the orchestrator stores in unix SECONDS, so `nowMs`
 * (Date.now(), ms) is converted per entry.
 */
import type { DagTask, TaskState } from './types';

export interface AttentionEntry {
  taskId: string;
  /** Best display label: title → goal → short id. */
  label: string;
  state: TaskState;
  /** Milliseconds since the task last changed (nowMs − updated_at·1000). */
  ageMs: number;
  /** Which group this entry belongs to (for the caller's convenience). */
  group: 'wants' | 'broken';
}

export interface Attention {
  /** Nodes waiting on the human (wants-steering or attached), oldest first. */
  wants: AttentionEntry[];
  /** Failed / abandoned nodes, oldest first. */
  broken: AttentionEntry[];
}

const BROKEN_STATES: ReadonlySet<TaskState> = new Set<TaskState>([
  'failed',
  'abandoned',
]);

function labelOf(t: DagTask): string {
  return (t.title || t.goal || '').trim() || `${t.task_id.slice(0, 8)}…`;
}

/**
 * Derive the two attention groups from the current task list. `nowMs` defaults
 * to `Date.now()` (injectable for tests). Each group is sorted oldest-first
 * (largest age) so the stalest, most-needing-attention node leads.
 */
export function deriveAttention(
  tasks: readonly DagTask[],
  nowMs: number = Date.now(),
): Attention {
  const wants: AttentionEntry[] = [];
  const broken: AttentionEntry[] = [];

  for (const t of tasks) {
    if (t.is_root) continue;
    const ageMs = nowMs - (t.updated_at ?? 0) * 1000;

    if (BROKEN_STATES.has(t.state)) {
      broken.push({ taskId: t.task_id, label: labelOf(t), state: t.state, ageMs, group: 'broken' });
      continue;
    }
    // A broken node is only ever "broken" (not also "wants"): terminal work
    // isn't a live steering session. Everything else can want the human.
    if (t.steer_requested || t.attached) {
      wants.push({ taskId: t.task_id, label: labelOf(t), state: t.state, ageMs, group: 'wants' });
    }
  }

  const byAge = (a: AttentionEntry, b: AttentionEntry) => b.ageMs - a.ageMs;
  wants.sort(byAge);
  broken.sort(byAge);
  return { wants, broken };
}
