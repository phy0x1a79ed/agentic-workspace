<script lang="ts">
  import { statusColor } from '$lib/theme/colors';
  interface Props { status: string }
  let { status }: Props = $props();
  const color = $derived(statusColor(status));
</script>

<span class="status-tag" style="color: {color}; border-color: color-mix(in oklab, {color} 40%, transparent); background: color-mix(in oklab, {color} 10%, transparent);">
  {status}
</span>

<style>
  .status-tag {
    font-family: var(--mono);
    font-size: 8px;
    letter-spacing: 1px;
    padding: 3px 8px;
    border-radius: 3px;
    text-transform: uppercase;
    border: 1px solid;
    display: inline-flex;
    align-items: center;
    white-space: nowrap;
  }
</style>
