import type { ComponentProps } from 'svelte';
import Component from './PttComposer.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  empty: {
    onsend: (text) => console.log('PTT send:', text),
  },

  // Atomic non-editable pills only — the PTT-only composition case.
  'with-chunks': {
    initialChunks: ['hello world', 'this is a second utterance'],
    onsend: (text) => console.log('PTT send:', text),
  },

  // Pills interleaved with typed text. Exercises the tree-walk for onsend
  // and the keyboard-Backspace-atomically-removes-prior-pill path.
  mixed: {
    initialChunks: [
      { chunk: 'morning', text: ' then I typed this bit ' },
      { chunk: 'and another dictation' },
    ],
    onsend: (text) => console.log('PTT send:', text),
  },
};

export { Component as component };
export default fixtures;
