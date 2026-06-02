import type { ComponentProps } from 'svelte';
import Component from './PttTab.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  empty: {},

  // Atomic non-editable pills only — the PTT-only composition case.
  'with-chunks': {
    initialChunks: ['hello world', 'this is a second utterance'],
  },

  // Pills interleaved with typed text. Exercises the tree-walk for walkText()
  // and the keyboard-Backspace-atomically-removes-prior-pill path.
  mixed: {
    initialChunks: [
      { chunk: 'morning', text: ' then I typed this bit ' },
      { chunk: 'and another dictation' },
    ],
  },
};

export { Component as component };
export default fixtures;
