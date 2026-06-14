<script lang="ts">
  /**
   * Agent convo page — a thin mount of <AgentChat>.
   *
   * All behavior (connect bar, spawn, live agent subscription, STT input via
   * @awm/ptt-composer, transcript + TTS via @awm/tts-history) lives inside
   * @awm/agent-chat. The page only mounts the component; it carries no business
   * logic. Project/scope come from ?project=…&scope=… (with localStorage
   * fallback), resolved inside the component.
   */
  import { AgentChat } from '@awm/agent-chat';
</script>

<AgentChat />
