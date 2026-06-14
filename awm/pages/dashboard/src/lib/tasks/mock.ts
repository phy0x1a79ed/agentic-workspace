import type { Task } from './types';

// Static fixtures so the board layout renders with realistic content. These
// vanish the moment USE_MOCKS flips false in client.ts and a real /tasks
// backend answers. Timestamps are hard-coded strings (no Date.now in build).

const T = (over: Partial<Task> & Pick<Task, 'id' | 'projectId' | 'title' | 'status'>): Task => ({
  acceptanceCriteria: [],
  assignedScope: null,
  dependsOn: [],
  result: null,
  needsHuman: false,
  createdAt: '2026-06-12T09:00:00Z',
  updatedAt: '2026-06-12T12:00:00Z',
  ...over
});

export const MOCK_TASKS: Task[] = [
  // payments — a project in trouble: every remaining task errored.
  T({
    id: 'pay-1',
    projectId: 'payments',
    title: 'Migrate ledger schema to v40',
    status: 'error',
    assignedScope: 'payments/ledger-migrate',
    acceptanceCriteria: ['existing rows preserved', 'down-migration tested'],
    result: { success: false, summary: 'alembic check failed on FK to retired table' },
    needsHuman: true
  }),
  T({
    id: 'pay-2',
    projectId: 'payments',
    title: 'Reconcile webhook retries',
    status: 'error',
    assignedScope: 'payments/webhook-retry',
    acceptanceCriteria: ['idempotent on duplicate delivery'],
    result: { success: false, summary: 'race on concurrent retry; needs lock' }
  }),
  T({
    id: 'pay-3',
    projectId: 'payments',
    title: 'Backfill currency codes',
    status: 'blocked',
    dependsOn: ['pay-1'],
    acceptanceCriteria: ['all NULL currencies resolved']
  }),

  // infra — mixed; one blocked, one awaiting review, work flowing.
  T({
    id: 'inf-1',
    projectId: 'infra',
    title: 'Rotate hub TLS certs',
    status: 'running',
    assignedScope: 'infra/cert-rotate',
    acceptanceCriteria: ['zero-downtime swap', 'old cert revoked']
  }),
  T({
    id: 'inf-2',
    projectId: 'infra',
    title: 'Pin CI node version',
    status: 'completed',
    assignedScope: 'infra/ci-node-pin',
    acceptanceCriteria: ['lockfile committed', 'CI green'],
    result: { success: true, summary: 'pinned to node 26.2.0; CI passing' },
    needsHuman: true // awaiting review
  }),
  T({
    id: 'inf-3',
    projectId: 'infra',
    title: 'Add log rotation for awm.log',
    status: 'blocked',
    dependsOn: ['inf-1'],
    acceptanceCriteria: ['logs capped at 100MB']
  }),
  T({ id: 'inf-4', projectId: 'infra', title: 'Document port bands', status: 'queued' }),

  // cyanoverse — calm; mostly queued.
  T({ id: 'cy-1', projectId: 'cyanoverse', title: 'Regenerate growth-curve figure', status: 'queued' }),
  T({ id: 'cy-2', projectId: 'cyanoverse', title: 'Update strain metadata', status: 'completed',
      result: { success: true, summary: 'merged' } })
];

export const MOCK_PROJECTS = ['payments', 'infra', 'cyanoverse'];
