<script lang="ts">
  /**
   * Status string → themed <Tag>. Maps the lowercase status to a primitive
   * tone; falls back to neutral. Used by the rooms list, agent rail, etc.
   */
  import Tag from '$lib/components/primitives/Tag.svelte';

  interface Props { status: string }
  let { status }: Props = $props();

  function toneFor(s: string): 'neutral' | 'ok' | 'warn' | 'danger' | 'atomizer' | 'mgr' {
    switch ((s || '').toLowerCase()) {
      case 'active':
      case 'running':    return 'atomizer';
      case 'verifying':  return 'warn';
      case 'completed':
      case 'done':       return 'ok';
      case 'failed':
      case 'cancelled':  return 'danger';
      case 'recording':
      case 'planning':   return 'mgr';
      default:           return 'neutral';
    }
  }

  const tone = $derived(toneFor(status));
</script>

<Tag {tone}>{status}</Tag>
