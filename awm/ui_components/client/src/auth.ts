/**
 * apiFetch + identity helpers — one fetch wrapper for every page that
 * talks to the hub. Sets `credentials: 'include'` so the awm_session
 * cookie is sent, attaches `X-Awm-As: user:<n>` derived from the
 * `awm_as` cookie the hub stamps on bootstrap, JSON-encodes object
 * bodies, and turns non-2xx responses into typed errors so callers can
 * distinguish auth failure (401/403) from anything else.
 */

let _awmAsCache = '';

/** Memoized `X-Awm-As` header value. Reads the `awm_as` cookie once. */
export function awmAs(): string {
  if (_awmAsCache) return _awmAsCache;
  const m = document.cookie.match(/(?:^|; )awm_as=([^;]*)/);
  _awmAsCache = 'user:' + (m ? decodeURIComponent(m[1]) : 'operator');
  return _awmAsCache;
}

export class HttpError extends Error {
  status: number;
  path: string;
  body: string;
  constructor(status: number, path: string, body: string) {
    super(`HTTP ${status} ${path}: ${body.slice(0, 240)}`);
    this.name = 'HttpError';
    this.status = status;
    this.path = path;
    this.body = body;
  }
}

/** Thrown on 401/403 so callers can branch on `instanceof AuthError`. */
export class AuthError extends HttpError {
  constructor(status: number, path: string, body: string) {
    super(status, path, body);
    this.name = 'AuthError';
  }
}

export interface ApiFetchInit extends Omit<RequestInit, 'body'> {
  /** Object → JSON-encoded; string/FormData/etc → passed through. */
  body?: unknown;
}

export async function apiFetch<T = unknown>(path: string, init: ApiFetchInit = {}): Promise<T> {
  const headers = new Headers(init.headers ?? {});
  // Default the caller identity to the operator (`user:<n>`), but let a caller
  // override it explicitly — an attached chat acts AS its placement's unit slug
  // so the agents service's attach-gated admin relay resolves the right caller.
  if (!headers.has('X-Awm-As')) headers.set('X-Awm-As', awmAs());

  let body: BodyInit | null | undefined = undefined;
  if (init.body !== undefined && init.body !== null) {
    if (
      typeof init.body === 'string' ||
      init.body instanceof FormData ||
      init.body instanceof Blob ||
      init.body instanceof ArrayBuffer ||
      init.body instanceof URLSearchParams ||
      ArrayBuffer.isView(init.body)
    ) {
      body = init.body as BodyInit;
    } else {
      if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
      body = JSON.stringify(init.body);
    }
  }

  const r = await fetch(path, {
    ...init,
    credentials: init.credentials ?? 'include',
    headers,
    body,
  });

  if (!r.ok) {
    const text = await r.text().catch(() => '');
    if (r.status === 401 || r.status === 403) {
      throw new AuthError(r.status, path, text);
    }
    throw new HttpError(r.status, path, text);
  }

  if (r.status === 204) return undefined as unknown as T;
  const text = await r.text();
  return (text ? JSON.parse(text) : null) as T;
}

/** GET /auth/whoami. Throws AuthError if not signed in. */
export async function whoami(): Promise<{ user: string; [k: string]: unknown }> {
  return await apiFetch<{ user: string }>('/auth/whoami', { method: 'GET' });
}
