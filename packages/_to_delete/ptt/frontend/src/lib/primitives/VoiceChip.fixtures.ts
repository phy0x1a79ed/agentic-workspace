import type { ComponentProps } from 'svelte';
import Component from './VoiceChip.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  idle: {
    text: 'hello world',
    onremove: () => console.log('VoiceChip remove'),
  },
  live: {
    text: '',
    live: true,
    onremove: () => console.log('VoiceChip remove'),
  },
  'live-with-text': {
    text: 'a partial that is mid-stream',
    live: true,
    onremove: () => console.log('VoiceChip remove'),
  },
  'long-text': {
    text: 'a much longer utterance that should wrap inside the chip without breaking the layout or the delete button positioning at the right edge',
    onremove: () => console.log('VoiceChip remove'),
  },
};

export { Component as component };
export default fixtures;
