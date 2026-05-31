/** Same-origin in dev (Vite proxies) and prod (uvicorn serves SPA + API). */
export const API_BASE = '';

/** WebSocket URL — relative, same host. Caller appends query params if needed. */
export function wsUrl(path = '/ws'): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}${path}`;
}

/** Browser feature flags for UI behaviour. */
export const SUPPORTS_TOUCH = typeof window !== 'undefined'
  && (('ontouchstart' in window) || (navigator.maxTouchPoints ?? 0) > 0);
