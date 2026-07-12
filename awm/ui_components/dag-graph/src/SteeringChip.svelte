<!--
  SteeringChip — the compact, read-only steering-state indicator, derived from a
  task's durable poll bits via `deriveSteering`. One headline chip
  (viewing / wants steering / attached / detaching…) plus small sub-markers for
  the two consent bits while attached (agent ready · you: done). Purely
  presentational — the actual attach / hand-off controls live beside it in the
  host (FocusPanel, chat header).
-->
<script lang="ts">
  import { Tag } from '@awm/primitives';
  import { deriveSteering, type SteerFields } from './steering';

  interface Props {
    /** The steering bits (a DagTask is structurally compatible). */
    task: SteerFields;
    /** Show the sub-markers (agent-ready / you-done) while attached. Default true. */
    detail?: boolean;
  }
  let { task, detail = true }: Props = $props();

  const chip = $derived(deriveSteering(task));
</script>

<span class="steer" data-state={chip.state}>
  <Tag tone={chip.tone}>{chip.label}</Tag>
  {#if detail && chip.attached && chip.state !== 'detaching'}
    {#if chip.agentReady}<span class="sub ready" title="the agent is ready to detach">agent ready</span>{/if}
    {#if chip.userDone}<span class="sub done" title="you have said you are done">you: done</span>{/if}
  {/if}
</span>

<style>
  .steer { display: inline-flex; align-items: center; gap: var(--space-1); flex-wrap: wrap; }
  .sub {
    font-family: var(--mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase;
    border-radius: var(--radius-md); padding: 0 var(--space-2);
  }
  .sub.ready { color: var(--ok); background: color-mix(in oklab, var(--ok) 14%, transparent); }
  .sub.done { color: var(--atomizer); background: color-mix(in oklab, var(--atomizer) 14%, transparent); }
</style>
