import type { ComponentProps } from 'svelte';
import Component from './PttPanel.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  // Live panel — expects sandbox hub + ptt svc + a token in localStorage.
  // Hold SPACE (or the on-screen button), speak, release → transcript lands.
  wired: {},

  // Offline mode — skips WS/mic setup entirely. Useful for laying out the
  // textbox without hitting the network or asking for mic permission.
  mocked: {
    mockEntries: [
      'mock A — laying out without mic',
      'mock B — second line so wrap is visible',
    ],
  },

  // Offline streaming — drives a scripted walk of `currentPartial` so the
  // in-progress render path and the splicing-revision case (second step
  // adds a previously-committed clause, third step revises the tail) can
  // be eyeballed at /dev/components/PttPanel?v=streaming and mounted by
  // the vitest crash-on-mount runner.
  streaming: {
    mockEntries: ['committed entry from earlier press'],
    mockPartialScript: [
      { delayMs: 600,  text: 'hello' },
      { delayMs: 1000, text: 'hello world' },
      { delayMs: 1000, text: 'hello world, this is' },
      { delayMs: 1000, text: 'hello world, this is the splicing demo' },
      { delayMs: 1200, text: null },
    ],
  },
};

export { Component as component };
export default fixtures;
