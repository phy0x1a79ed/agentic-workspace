/**
 * Page-local key/value state, served by the tts service via
 *   POST /svc/tts/fn/{getState,setState,delState}
 */

const SVC = '/svc/tts';

async function callFn<T>(fn: string, args: unknown = {}): Promise<T> {
  const r = await fetch(`${SVC}/fn/${fn}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(args),
  });
  if (r.status === 404) return undefined as unknown as T;
  if (!r.ok) {
    const detail = await r.text().catch(() => '');
    throw new Error(`POST ${SVC}/fn/${fn} → HTTP ${r.status}: ${detail}`);
  }
  if (r.status === 204) return undefined as unknown as T;
  return (await r.json()) as T;
}

export async function getState<T = unknown>(key: string): Promise<T | null> {
  const r = await callFn<{ value: T | null }>('getState', { key });
  return r?.value ?? null;
}

export async function setState<T = unknown>(key: string, value: T): Promise<void> {
  await callFn<void>('setState', { key, value });
}

export async function deleteState(key: string): Promise<void> {
  await callFn<void>('delState', { key });
}
