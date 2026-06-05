"""Tiny HTTP login bookmark for the dev harness.

Serves a single page on ``http://127.0.0.1:$AWM_DEV_LOGIN_PORT/`` (default
7822) that, on every load, calls the live awm server's ``/auth/mint``
endpoint and renders one big clickable link to the resulting one-shot
``/auth/bootstrap?ot=...`` URL.

Why a second port instead of a route on the main app:

- ``/auth/mint`` requires a Bearer header (the loopback token).  We don't
  want to expose that token to the browser, so the mint has to happen
  server-side.
- Adding the route to ``awm.server`` would mean modifying production
  code for a dev-only convenience.

The login bookmark itself doesn't carry secrets — only the (single-use,
60s TTL) nonce URL is rendered, so plain HTTP is fine here.

Run via ``dev/run.sh start`` (which launches this alongside uvicorn). Or
manually:

    AWM_WORKSPACE=$(pwd)/dev AWM_PORT=7821 \\
        mamba run -n awm python dev/login_server.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".awm" / "auth.token"

AWM_HOST = "127.0.0.1"
AWM_PORT = int(os.environ.get("AWM_PORT", "7821"))
LOGIN_HOST = os.environ.get("AWM_DEV_LOGIN_HOST", "127.0.0.1")
LOGIN_PORT = int(os.environ.get("AWM_DEV_LOGIN_PORT", "7822"))
LOGIN_USER = os.environ.get("AWM_DEV_USER", "dev")
# When the bookmark page is reached from a non-loopback browser, /auth/mint
# must still be called via loopback (it 403s otherwise), but the rendered
# bootstrap URL needs the publicly-reachable host. Set AWM_DEV_PUBLIC_HOST
# to the address the browser will use.
PUBLIC_HOST = os.environ.get("AWM_DEV_PUBLIC_HOST", "")


def _mint_url() -> tuple[str | None, str | None]:
    """Return (url, error). Exactly one is non-None."""
    try:
        token = TOKEN_FILE.read_text().strip()
    except FileNotFoundError:
        return None, f"auth.token not found at {TOKEN_FILE} — has the harness started?"
    if not token:
        return None, "auth.token is empty"

    body = json.dumps({"awm_user": LOGIN_USER}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{AWM_HOST}:{AWM_PORT}/auth/mint",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return None, f"could not reach the awm server at {AWM_HOST}:{AWM_PORT} ({e})"
    except Exception as e:  # noqa: BLE001 — surface anything to the page
        return None, f"unexpected error from /auth/mint: {e}"

    url = payload.get("url")
    if not url:
        return None, f"/auth/mint returned no url: {payload!r}"
    if PUBLIC_HOST:
        url = url.replace(
            f"http://{AWM_HOST}:{AWM_PORT}/",
            f"http://{PUBLIC_HOST}:{AWM_PORT}/",
            1,
        )
    return url, None


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>awm dev — login</title>
<style>
  body {{
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    background: #111; color: #eee; margin: 0;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 32px;
  }}
  main {{ max-width: 640px; text-align: center; }}
  h1 {{ font-size: 14px; color: #888; text-transform: uppercase;
        letter-spacing: 0.2em; margin: 0 0 24px; }}
  a.login {{
    display: inline-block; padding: 18px 36px; border: 1px solid #4af;
    color: #4af; text-decoration: none; font-size: 18px;
    border-radius: 6px; transition: background 0.15s;
  }}
  a.login:hover {{ background: #4af; color: #111; }}
  p.hint {{ color: #666; font-size: 12px; margin-top: 28px;
            line-height: 1.6; }}
  p.err {{ color: #f88; font-size: 13px; max-width: 540px;
           margin: 0 auto; }}
  code {{ color: #ccc; }}
</style>
</head>
<body>
<main>
  <h1>awm dev login</h1>
  {body}
  <p class="hint">
    refresh this page for a fresh link (each is single-use, 60s ttl).
  </p>
</main>
</body>
</html>
"""


def _render(url: str | None, err: str | None) -> bytes:
    if url:
        body = f'<a class="login" href="{url}">log in &rarr; /ui/</a>'
    else:
        body = f'<p class="err">{err}</p>'
    return _PAGE.format(body=body).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — stdlib API
        if self.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        url, err = _mint_url()
        page = _render(url, err)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        sys.stderr.write("[login-server] " + (fmt % args) + "\n")


def main() -> int:
    server = ThreadingHTTPServer((LOGIN_HOST, LOGIN_PORT), Handler)
    print(f"[login-server] http://{LOGIN_HOST}:{LOGIN_PORT}/  "
          f"(mints against http://{AWM_HOST}:{AWM_PORT})",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
