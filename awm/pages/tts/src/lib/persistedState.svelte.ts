/**
 * persistedState — a Svelte 5 reactive value backed by the stripe
 * /state surface. Hydrates from the server on creation, writes back
 * 300ms after the last local mutation, and protects against the
 * common late-GET race where a slow network response would otherwise
 * stomp a user-initiated change.
 *
 * Usage:
 *   const vol = persistedState<number>('volume', 1.0);
 *   // read: vol.value
 *   // write: vol.value = 0.5
 *   // reactive across components via prop passing
 *
 * The `.svelte.ts` extension is what tells Svelte's compiler to
 * process the `$state` rune in this file.
 */
import { onDestroy } from 'svelte';
import { getState, setState } from '$lib/api/state';

const DEBOUNCE_MS = 300;

export interface Persisted<T> {
  readonly key: string;
  value: T;
  /** True once the initial hydration GET has resolved (success or fail). */
  readonly ready: boolean;
}

export function persistedState<T>(key: string, initial: T): Persisted<T> {
  let inner = $state<T>(initial);
  let ready = $state(false);
  let touched = false;
  let debounce: ReturnType<typeof setTimeout> | null = null;
  let pendingValue: T | null = null;

  function flush(): void {
    if (debounce) {
      clearTimeout(debounce);
      debounce = null;
    }
    if (pendingValue === null) return;
    const v = pendingValue;
    pendingValue = null;
    setState(key, v).catch((err) => console.warn(`[persistedState] ${key}:`, err));
  }

  // Hydrate from the server. Don't await — let the component mount.
  getState<T>(key)
    .then((server) => {
      if (server !== null && !touched) inner = server;
    })
    .catch((err) => console.warn(`[persistedState] ${key} hydrate:`, err))
    .finally(() => { ready = true; });

  onDestroy(() => { flush(); });

  return {
    key,
    get value(): T { return inner; },
    set value(v: T) {
      touched = true;
      inner = v;
      pendingValue = v;
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => {
        debounce = null;
        flush();
      }, DEBOUNCE_MS);
    },
    get ready(): boolean { return ready; },
  };
}
