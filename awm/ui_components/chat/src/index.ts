export { default as Chat } from './Chat.svelte';
export { makeSubmitGate, type SubmitGateOpts } from './submit-gate';
// Re-exported for consumers so they can type `posts` and drive replay/playback
// without also depending on @awm/tts-history directly.
export {
  type Post,
  TtsCall,
  listEngines,
  playOnce,
  type EngineDescriptor,
  type EngineRegistry,
} from '@awm/tts-history';
