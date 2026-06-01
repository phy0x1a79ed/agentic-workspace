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
};

export { Component as component };
export default fixtures;
