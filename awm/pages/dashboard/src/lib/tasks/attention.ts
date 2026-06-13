import type { ProjectAttention, ScopeCounts, Task, TaskStatus } from './types';

// Attention scoring: how badly does a project need the operator's eyes?
//
// The user's worked example — "all remaining tasks are in error" — should
// float a project to the top. So error dominates, blocked and needs-human
// pull up, and a high completed ratio pulls down (work is winding down).

export interface AttentionInput {
  breakdown?: Partial<Record<TaskStatus, number>>;
  needsHuman?: number;
  scopeCounts?: ScopeCounts;
}

export function attentionScore(input: AttentionInput): number {
  const b = input.breakdown ?? {};
  const total =
    (b.queued ?? 0) +
    (b.running ?? 0) +
    (b.blocked ?? 0) +
    (b.error ?? 0) +
    (b.completed ?? 0);

  // No task data yet → fall back to active-scope pressure so the list still
  // sorts meaningfully before a tasks backend exists.
  if (total === 0) {
    return clamp((input.scopeCounts?.active ?? 0) * 5);
  }

  const ratio = (n: number) => n / total;
  const score =
    100 * ratio(b.error ?? 0) +
    60 * ratio(b.blocked ?? 0) +
    25 * ratio(input.needsHuman ?? 0) -
    20 * ratio(b.completed ?? 0);

  return clamp(score);
}

function clamp(n: number): number {
  return Math.max(0, Math.min(100, Math.round(n)));
}

/** Tally a task list into the per-lane breakdown + needs-human count. */
export function breakdownOf(tasks: Task[]): {
  breakdown: Record<TaskStatus, number>;
  needsHuman: number;
} {
  const breakdown: Record<TaskStatus, number> = {
    queued: 0,
    running: 0,
    blocked: 0,
    error: 0,
    completed: 0
  };
  let needsHuman = 0;
  for (const t of tasks) {
    breakdown[t.status] += 1;
    if (t.needsHuman) needsHuman += 1;
  }
  return { breakdown, needsHuman };
}

/** Sort projects most-in-need-of-attention first; tie-break newest-touched. */
export function byAttention(a: ProjectAttention, b: ProjectAttention): number {
  return b.attentionScore - a.attentionScore || a.name.localeCompare(b.name);
}
