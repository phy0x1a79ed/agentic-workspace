import type { ComponentProps } from 'svelte';
import Component from './ConvoTab.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  empty: {},

  'with-chips': {
    initialChips: [
      'hello world',
      'and a second utterance',
      'the third one came after a short pause',
    ],
  },
};

export { Component as component };
export default fixtures;
