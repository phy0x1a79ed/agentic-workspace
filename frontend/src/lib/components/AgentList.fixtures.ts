import type { ComponentProps } from 'svelte';
import type { RoomAgent } from '$lib/api/client';
import Component from './AgentList.svelte';

// `single` and `many` reproduce the composition-seam crash that hides every
// agent on /focus: AgentList line 50 binds `expanded={expanded[a.scope]}`
// against AgentCard's `expanded = $bindable(false)` declaration. The first
// time the scope key isn't in the `expanded` record, the parent writes
// `undefined` through the bind — props_invalid_value.
//
// The failing variants ARE the bug tracker. Fix lands in AgentList.svelte
// (either initialize the record per agent or change AgentCard's bindable
// shape); both variants pass once the bind seam is sound.

const liveActive = {
  pid: 12345,
  status: 'running',
  agent_cli: 'claude',
  started_at: '2026-05-29T20:00:00Z',
};

const agentScope = (scope: string, overrides: Partial<RoomAgent> = {}): RoomAgent => ({
  scope,
  kind: 'scope',
  live: liveActive,
  ...overrides,
});

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  empty: { agents: [] },
  single: { agents: [agentScope('user/dev')] },
  many: {
    agents: [
      agentScope('user/manager', { kind: 'scope' }),
      agentScope('user/dev'),
      agentScope('remote/peer-1', { kind: 'shadow_peer', live: null }),
    ],
  },
  noLive: { agents: [agentScope('user/idle', { live: null })] },
};

export { Component as component };
export default fixtures;
