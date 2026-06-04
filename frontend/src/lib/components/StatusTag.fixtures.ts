import type { ComponentProps } from 'svelte';
import Component from './StatusTag.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  active: { status: 'active' },
  verifying: { status: 'verifying' },
  completed: { status: 'completed' },
  failed: { status: 'failed' },
  recording: { status: 'recording' },
  unknown: { status: 'unknown-state' },
};

export { Component as component };
export default fixtures;
