import type { ComponentProps } from 'svelte';
import Component from './PttPanel.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  // Live panel — expects sandbox hub + ptt svc + a session cookie.
  // PTT tab: hold SPACE (or the on-screen button), speak, release → chunk
  // lands at the caret. Convo tab: tap PTT to toggle a continuous session,
  // backend auto-cuts on silence into individual chips.
  wired: {
    onsend: (text) => console.log('PTT send:', text),
  },

  // Offline — seeds both tabs so the layout can be inspected without
  // hitting the network or asking for mic permission.
  mocked: {
    mockInitial: [
      { chunk: 'mock utterance A', text: ' then some typing ' },
      'mock utterance B (second chunk)',
    ],
    mockInitialChips: [
      'first hands-free utterance',
      'second one after a short pause',
    ],
    onsend: (text) => console.log('PTT send:', text),
  },

  // Offline streaming — drives a scripted walk of partial→finalize so
  // the live-pill / live-chip rendering can be eyeballed at
  // /dev/components/PttPanel?v=streaming and mounted by the vitest
  // crash-on-mount runner. Target is whichever tab is active.
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
