"""Entrypoint for the mira API daemon — ``python -m mira_api``.

Binds the awm-network interface only (NOT 0.0.0.0), serves over TLS, and
authenticates every request with a bearer token. All knobs come from the
environment so the systemd unit is the single source of truth:

    MIRA_API_HOST        bind address              (default 172.16.0.24)
    MIRA_API_PORT        bind port                 (default 7822)
    MIRA_CDP_BASE        Opera CDP http base       (default http://127.0.0.1:9224)
    MIRA_PLATFORMS       comma list                (default slack,teams)
    MIRA_STORAGE         cloud file stores         (default empty; e.g. onedrive)
    MIRA_POLL_INTERVAL   watcher seconds           (default 15)
    MIRA_TLS_CERT        cert PEM    (default ~/.awm/tls/cert.pem)
    MIRA_TLS_KEY         key PEM     (default ~/.awm/tls/key.pem)
    MIRA_AUTH_TOKEN      inline bearer token, OR
    MIRA_AUTH_TOKEN_FILE token file  (default ~/.awm/auth.token)

With no TLS cert present the daemon still starts over plain HTTP (dev only) and
logs a warning — production always has the certs.
"""

from __future__ import annotations

import logging
import os
import ssl
import sys
from pathlib import Path

from aiohttp import web

from mira_api.server import MiraAPI

log = logging.getLogger("mira_api")


def _read_token() -> str:
    inline = os.environ.get("MIRA_AUTH_TOKEN")
    if inline and inline.strip():
        return inline.strip()
    ref = os.environ.get("MIRA_AUTH_TOKEN_FILE") or str(
        Path.home() / ".awm" / "auth.token")
    try:
        return Path(ref).read_text().strip()
    except OSError as exc:
        log.error("cannot read auth token from %s: %s", ref, exc)
        sys.exit(2)


def _ssl_context() -> ssl.SSLContext | None:
    cert = os.environ.get("MIRA_TLS_CERT") or str(
        Path.home() / ".awm" / "tls" / "cert.pem")
    key = os.environ.get("MIRA_TLS_KEY") or str(
        Path.home() / ".awm" / "tls" / "key.pem")
    if not (Path(cert).exists() and Path(key).exists()):
        log.warning("TLS cert/key not found (%s); serving PLAIN HTTP (dev only)", cert)
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = os.environ.get("MIRA_API_HOST", "172.16.0.24")
    port = int(os.environ.get("MIRA_API_PORT", "7822"))
    cdp_base = os.environ.get("MIRA_CDP_BASE", "http://127.0.0.1:9224")
    platforms = [p.strip() for p in
                 os.environ.get("MIRA_PLATFORMS", "slack,teams").split(",") if p.strip()]
    storage = [s.strip() for s in
               os.environ.get("MIRA_STORAGE", "").split(",") if s.strip()]
    poll = float(os.environ.get("MIRA_POLL_INTERVAL", "15"))
    token = _read_token()

    api = MiraAPI(cdp_base, platforms, token, poll_interval=poll, storage=storage)
    app = api.build_app()
    scheme = "https"
    ctx = _ssl_context()
    if ctx is None:
        scheme = "http"
    log.info("mira-api serving %s://%s:%d (platforms=%s, storage=%s, cdp=%s)",
             scheme, host, port, ",".join(platforms),
             ",".join(storage) or "-", cdp_base)
    web.run_app(app, host=host, port=port, ssl_context=ctx, print=None)


if __name__ == "__main__":
    main()
