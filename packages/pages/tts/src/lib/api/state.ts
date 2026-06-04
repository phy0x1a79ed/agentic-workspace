/**
 * Page-local key/value state, served by the tts service via
 *   POST /svc/tts/fn/{getState,setState,delState}
 */

import { svc, HttpError } from '@awm/client';

const TTS = svc('tts');

export async function getState<T = unknown>(key: string): Promise<T | null> {
  try {
    const r = await TTS.fn<{ value: T | null }>('getState', { key });
    return r?.value ?? null;
  } catch (err) {
    // The state surface returns 404 when the key was never set — treat
    // as "no value" rather than an error so callers can use the
    // returned-default-on-null contract.
    if (err instanceof HttpError && err.status === 404) return null;
    throw err;
  }
}

export async function setState<T = unknown>(key: string, value: T): Promise<void> {
  await TTS.fn<void>('setState', { key, value });
}

export async function deleteState(key: string): Promise<void> {
  await TTS.fn<void>('delState', { key });
}
