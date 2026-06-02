import type { ComponentProps } from 'svelte';
import Component from './PttPanel.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  // Live panel — expects sandbox hub + ptt svc + a session cookie.
  // Hold SPACE (or the on-screen button), speak, release → chunk lands
  // at the caret. Type freely; mix and match.
  wired: {
    onsend: (text) => console.log('PTT send:', text),
  },

  // Offline mode — skips WS/mic setup entirely. Useful for laying out
  // the composer without hitting the network or asking for mic permission.
  mocked: {
    mockInitial: [
      { chunk: 'mock utterance A', text: ' then some typing ' },
      'mock utterance B (second chunk)',
    ],
    onsend: (text) => console.log('PTT send:', text),
  },

  // Offline streaming — drives a scripted walk of `currentPartial` so the
  // banner UI can be eyeballed at /dev/components/PttPanel?v=streaming and
  // mounted by the vitest crash-on-mount runner.
  streaming: {
    mockInitial: ['committed entry from earlier press'],
    mockPartialScript: [
      { delayMs: 600,  text: 'hello' },
      { delayMs: 1000, text: 'hello world' },
      { delayMs: 1000, text: 'hello world, this is' },
      { delayMs: 1000, text: 'hello world, this is the splicing demo' },
      { delayMs: 1200, text: null },
    ],
    onsend: (text) => console.log('PTT send:', text),
  },
};

export { Component as component };
export default fixtures;
