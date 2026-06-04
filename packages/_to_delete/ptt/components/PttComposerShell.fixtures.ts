import type { ComponentProps } from 'svelte';
import Component from './PttComposerShell.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  empty: {
    onsend: (text) => console.log('Shell send:', text),
  },

  'ptt-tab-with-chunks': {
    initialChunks: ['hello world', 'second utterance'],
    onsend: (text) => console.log('Shell send:', text),
  },

  'convo-tab-with-chips': {
    initialChips: [
      'first dictated utterance',
      'and another one',
      'a third after a pause',
    ],
    onsend: (text) => console.log('Shell send:', text),
  },
};

export { Component as component };
export default fixtures;
