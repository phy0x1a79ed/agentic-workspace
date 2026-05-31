import type { ComponentProps } from 'svelte';
import Component from './TranscriptHistory.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  empty: {
    entries: [],
  },
  one: {
    entries: ['hello world'],
  },
  many: {
    entries: [
      'first utterance',
      'second',
      'third',
      'fourth',
      'fifth',
      'sixth',
      'seventh',
      'eighth',
      'ninth',
      'tenth',
    ],
  },
  long: {
    entries: [
      'this is a single very long transcript that should wrap across multiple lines without breaking the layout — wrapping behaviour and word-break on a continuous stream of words like this should remain readable inside the scrolling panel',
    ],
  },
};

export { Component as component };
export default fixtures;
