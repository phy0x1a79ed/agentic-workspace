import type { ComponentProps } from 'svelte';
import Component from './PttButton.svelte';

const log = (label: string) => () => {
  // eslint-disable-next-line no-console
  console.log(`[PttButton] ${label}`);
};

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  idle: {
    onpttdown: log('down'),
    onpttup: log('up'),
  },
  disabled: {
    disabled: true,
  },
  'no-handlers': {},
};

export { Component as component };
export default fixtures;
