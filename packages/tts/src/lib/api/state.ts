/**
 * Stripe-local UI state — generic JSON k/v over the TTS backend's
 * /state surface. The mirror of the api/tts.ts presets client; thin
 * wrappers around fetch with relative URLs so the bundle stays
 * location-agnostic across hub prefix changes.
 */

export async function getState<T = unknown>(key: string): Promise<T | null> {
  const r = await fetch(`./_api/state/${encodeURIComponent(key)}`, { credentials: 'include' });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`GET /state/${key} → HTTP ${r.status}`);
  const body = (await r.json()) as { value: T };
  return body.value;
}

export async function setState<T = unknown>(key: string, value: T): Promise<void> {
  const r = await fetch(`./_api/state/${encodeURIComponent(key)}`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ value }),
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => '');
    throw new Error(`PUT /state/${key} → HTTP ${r.status}: ${detail}`);
  }
}

export async function deleteState(key: string): Promise<void> {
  const r = await fetch(`./_api/state/${encodeURIComponent(key)}`, {
    method: 'DELETE',
    credentials: 'include',
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => '');
    throw new Error(`DELETE /state/${key} → HTTP ${r.status}: ${detail}`);
  }
}
