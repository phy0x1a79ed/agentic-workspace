import type { ProjectAttention, Task } from './types';
import { attentionScore, breakdownOf, byAttention } from './attention';
import { MOCK_PROJECTS, MOCK_TASKS } from './mock';

// The seam. Today every call is served from mock fixtures. When the future
// /tasks backend exists (see CONTRACT.md), flip USE_MOCKS to false and add the
// apiFetch-backed branch here — NO board/view code changes, only this module.
const USE_MOCKS = true;

export async function listTasks(projectId: string): Promise<Task[]> {
  if (USE_MOCKS) return MOCK_TASKS.filter((t) => t.projectId === projectId);
  // return (await apiFetch(`/projects/${projectId}/tasks`)).tasks;
  throw new Error('tasks backend not wired');
}

export async function getTask(id: string): Promise<Task | null> {
  if (USE_MOCKS) return MOCK_TASKS.find((t) => t.id === id) ?? null;
  // return await apiFetch(`/tasks/${id}`);
  throw new Error('tasks backend not wired');
}

export async function retryTask(id: string): Promise<Task | null> {
  if (USE_MOCKS) return getTask(id); // inert in the mock round
  // return await apiFetch(`/tasks/${id}/retry`, { method: 'POST' });
  throw new Error('tasks backend not wired');
}

export async function resolveTask(id: string): Promise<Task | null> {
  if (USE_MOCKS) return getTask(id); // inert in the mock round
  // return await apiFetch(`/tasks/${id}/resolve`, { method: 'POST' });
  throw new Error('tasks backend not wired');
}

/**
 * Projects ranked by attention. Identity/counts will come from
 * GET /projects/search once wired; the score derives from task breakdown.
 */
export async function listProjectAttention(): Promise<ProjectAttention[]> {
  const out: ProjectAttention[] = MOCK_PROJECTS.map((name) => {
    const tasks = MOCK_TASKS.filter((t) => t.projectId === name);
    const { breakdown, needsHuman } = breakdownOf(tasks);
    const scopeCounts = { active: breakdown.running, completed: breakdown.completed, deleted: 0 };
    return {
      name,
      scopeCounts,
      taskBreakdown: breakdown,
      attentionScore: attentionScore({ breakdown, needsHuman, scopeCounts })
    };
  });
  return out.sort(byAttention);
}
