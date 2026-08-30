"""The off-host HTTPS reverse proxy — TLS front for the whole awm gateway.

The awm gateway binds **loopback plain HTTP** (``127.0.0.1:7819``) with no auth,
by design. But powerful browser APIs — ``getUserMedia`` for the notes-page
dictation, clipboard, etc. — need a *secure context*, which off-localhost means
HTTPS. So this listener terminates TLS on ``0.0.0.0:<port>`` and transparently
reverse-proxies **every** request — HTTP and WebSocket alike — to the loopback
gateway, so the entire awm surface (pages at ``/ui/*``, service RPC at
``/svc/*``, the hub control plane, config, …) is reachable over one HTTPS origin.

Because the notes page (and every awm page) makes *same-origin* relative calls
(``apiFetch('/svc/...')``, ``new WebSocket(<same-origin path>)``), fronting the
gateway wholesale is what makes those calls Just Work under HTTPS — there is no
per-path allowlist to keep in sync.

Two things the edge asserts on every proxied request: the caller is
authenticated, and ``X-Awm-As`` names the identity the session was minted
for (``user:<sub>``, or ``peer`` for a bearer). The browser's own value of
that header is discarded — downstream services trust it, so only the edge may
write it.

The public profile (``AWM_EDGE_PROFILE=public``) narrows the door to the
allow-list in :mod:`awm.httpsfront.policy` and, with ``AWM_EDGE_TLS=0``, serves
plain HTTP on loopback for a TLS-terminating nginx in front.

**Routing reads the decoded path; forwarding sends the raw one.** Both are
deliberate and neither may be "simplified" into the other. Every routing and
policy decision needs the *decoded* path, because that is what the caller
means; every upstream request needs the *raw* bytes, because rebuilding a URL
from the decoded string loses the difference between ``%23`` and ``#`` and
between ``%2e%2e`` and a directory climb. The gap between the two is closed by
refusing any target that re-segments when decoded — see :func:`_re_segments`.

Built on Starlette + uvicorn (TLS) with ``httpx`` for HTTP and the ``websockets``
client for WS bridging — all already present in the ``awm`` env (the gateway
depends on them). Runs in a daemon thread launched from the hub adapter's
``on_start``; when the gateway drains/respawns this service, the process exits
and the listener dies with it (one supervised lifetime, exactly like ``mic``).
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from awm.httpsfront import pages, penpot, policy, store, vault
from awm.httpsfront.auth import AS_COOKIE_NAME, COOKIE_NAME, PEER_SUB, AuthGate, bearer_of

log = logging.getLogger("awm.httpsfront.proxy")

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1); the client/httpx set
# their own. ``host`` is dropped so httpx derives it from the upstream URL.
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}

# Request methods the front forwards (a superset covering the awm surface).
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

_ALL_METHODS = _METHODS  # alias for route registration clarity

_PEER_NAME = socket.gethostname().split(".")[0].title()

# Routing reads the *decoded* path; forwarding sends the *raw* one. Those two
# facts are the whole of the encoding contract on this edge, and they are the
# kind of invariant that reads as an accident and gets "simplified" back into a
# bug — so they live together, here, with the reason.


def _raw_target(scope, decoded: str) -> bytes:
    """The request path exactly as it arrived, percent-encoding intact.

    Starlette's ``url.path`` is percent-*decoded*, so rebuilding an upstream URL
    out of it destroys the difference between ``%23`` and ``#``: httpx reparses
    the string it is handed and takes everything after a ``#`` as a fragment,
    which it never sends. Trilium's client escapes its search string into the
    path, so that alone truncated every attribute search to ``/api/search/``.

    ASGI keeps the original bytes in ``raw_path`` — uvicorn sets it on both its
    h11 and its httptools implementation, HTTP and WebSocket alike. A server
    that omits it leaves us re-encoding the decoded path, which is lossy in
    exactly this way but no worse than what it replaces.
    """
    raw = scope.get("raw_path")
    if raw:
        return raw.split(b"?", 1)[0]
    return quote(decoded).encode("ascii")


def _re_segments(raw: bytes, decoded: str) -> bool:
    """Whether this target means one thing to the router and another upstream.

    The two paths agree on everything except a target that *re-segments* when
    decoded. An encoded separator (``%2f``, ``%5c``) is one segment to the
    router and two to the upstream; a ``..`` — plain or arrived as ``%2e%2e`` —
    is a segment to the router and a level up to any client that normalises
    before sending, which is how ``/trilium/api/%2e%2e/etapi/app-info`` used to
    classify as the vault's and arrive as the unauthenticated ETAPI's.

    Refused outright rather than canonicalised: nothing this edge serves needs
    either form, and a 404 is the same answer every other path off the list
    gets.
    """
    low = raw.lower()
    if b"%2f" in low or b"%5c" in low:
        return True
    return ".." in decoded.split("/")


def _upstream_url(origin: str, raw_path: bytes, query: bytes) -> httpx.URL:
    """``origin`` + the target, byte for byte.

    ``copy_with(raw_path=…)`` sets the target verbatim instead of reparsing a
    string — the reparse is what swallowed the fragment and what normalised the
    ``..``. It carries the query with it, so nothing else may append one.
    """
    return httpx.URL(origin).copy_with(
        raw_path=raw_path + (b"?" + query if query else b""))


PUBLIC_PROFILE = "public"
# Where a signed-in browser lands on the public profile. The vault, because
# the vault is what this host is for. Kept as a redirect from `/` rather than
# serving the shell there: `/` is the one path the sign-in form reloads, and a
# `/` that fell through to the catch-all would proxy the gateway's own page
# index — an enumeration of the whole internal surface — to the internet.
PUBLIC_HOME = vault.SHELL


def _as_header(sub: str | None) -> str:
    return PEER_SUB if sub == PEER_SUB else f"user:{sub or 'operator'}"


def _origin_override(app) -> str | None:
    """The scheme+authority a rewriting front presents as ``Origin``, or None.

    ``_HOP`` drops the browser's ``Host`` so httpx derives it from the upstream
    URL — so an upstream that compares ``Origin`` against ``Host`` sees a pair
    that cannot match while ``Origin`` is forwarded verbatim. Rewriting it to
    the upstream's own origin restores the comparison the upstream is actually
    trying to make: same-origin, as it would be on loopback.
    """
    return getattr(app.state, "origin_override", None)


def _req_headers(request: Request, sub: str | None = None) -> dict[str, str]:
    hdrs = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    hdrs["X-Forwarded-Proto"] = "https"
    # Overwrite, never default: the browser's value is unverified.
    hdrs.pop("x-awm-as", None)
    hdrs["X-Awm-As"] = _as_header(sub)
    override = _origin_override(request.app)
    # Only rewrite a header the browser actually sent: minting an Origin where
    # there was none turns a same-origin navigation into a cross-origin one.
    if override and "origin" in hdrs:
        hdrs["origin"] = override
    # ``host`` is dropped above so httpx derives it from the upstream URL, which
    # means the upstream otherwise cannot tell what address the browser used.
    # awm's own services never needed that, but a reverse-proxied third party
    # does: an app that builds its self-origin from the request (for redirects,
    # or to decide the ``Secure`` flag on its own cookies) derives a loopback
    # http:// origin without this, and its browser session never persists.
    host = request.headers.get("host")
    if host:
        hdrs["X-Forwarded-Host"] = host
    if request.client:
        hdrs["X-Forwarded-For"] = request.client.host
    return hdrs


def _resp_headers(resp: httpx.Response) -> list[tuple[str, str]]:
    # Preserve everything except hop-by-hop; keep raw ``aiter_raw`` bytes so
    # content-encoding + content-length stay consistent (we pass bytes through).
    return [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in _HOP]


def _wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def _samesite(app) -> str:
    return "strict" if getattr(app.state, "profile", None) == PUBLIC_PROFILE else "lax"


def _set_session_cookie(resp: Response, token: str, max_age: int,
                        samesite: str = "lax") -> None:
    resp.set_cookie(
        COOKIE_NAME, token, max_age=max_age, path="/",
        httponly=True, secure=True, samesite=samesite,
    )


def _set_as_cookie(resp: Response, sub: str, max_age: int,
                   samesite: str = "lax") -> None:
    resp.set_cookie(
        AS_COOKIE_NAME, sub, max_age=max_age, path="/",
        httponly=False, secure=True, samesite=samesite,
    )


def _set_penpot_cookie(resp: Response, token: str, max_age: int,
                       samesite: str = "lax") -> None:
    """Hand the browser the Penpot session awm holds on this person's behalf.

    Same name, path and flags Penpot's own ``assign-session-cookie`` uses, so
    Penpot's renewal replaces this cookie rather than the browser ending up
    holding two at different paths. The lifetime is awm's session TTL rather
    than Penpot's much longer default: the Penpot identity is granted by the awm
    session and has no business outliving it.
    """
    resp.set_cookie(
        penpot.COOKIE_NAME, token, max_age=max_age, path="/",
        httponly=True, secure=True, samesite=samesite,
    )


def _clear_penpot_cookies(resp: Response) -> None:
    """Drop Penpot's session cookie when the awm identity changes.

    The same hole ``_clear_vault_cookies`` closes, with a worse consequence:
    Penpot's cookie *is* an identity, not a handle to a shared vault, so a
    browser that keeps it across a sign-out hands the next person to sign in the
    previous person's design files. The edge re-mints the right one on the next
    shell load, so clearing costs nothing.
    """
    resp.delete_cookie(penpot.COOKIE_NAME, path="/")


def _unpack(result) -> tuple[bool, str | None, str | None]:
    """``(ok, refreshed, sub)`` from a gate answer; a two-tuple (an older gate)
    means the operator."""
    ok, refreshed = result[0], result[1]
    sub = result[2] if len(result) > 2 else None
    return ok, refreshed, (sub or "operator") if ok else None


async def _authenticate(request: Request) -> tuple[bool, str | None]:
    """(ok, refreshed_cookie_token_or_None) for the current request."""
    ok, refreshed, _ = await _authenticate_sub(request)
    return ok, refreshed


async def _authenticate_sub(request: Request) -> tuple[bool, str | None, str | None]:
    """(ok, refreshed_cookie_token_or_None, sub) for the current request."""
    gate: AuthGate = request.app.state.gate
    return _unpack(await gate.authenticate(
        cookie=request.cookies.get(COOKIE_NAME),
        bearer=bearer_of(request.headers.get("authorization")),
    ))


def _is_public(app) -> bool:
    return getattr(app.state, "profile", None) == PUBLIC_PROFILE


def _not_found() -> Response:
    return Response("not found", status_code=404)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _site_name(request: Request) -> str:
    """What to call this node on the sign-in screen.

    The public host's own hostname is a droplet name nobody types; what a person
    typed is in ``Host``, and it is the only label that will match the address
    bar they are looking at. Off the public profile the peer name is right and
    the Host header is usually an IP.
    """
    if not _is_public(request.app):
        return _PEER_NAME
    host = (request.headers.get("host") or "").split(":")[0]
    label = host.split(".")[0]
    return label.title() if label else _PEER_NAME


def _deny(request: Request) -> Response:
    """Sign-in screen for a browser GET, else 401 (API / peer)."""
    if request.method == "GET" and _wants_html(request):
        return HTMLResponse(
            pages.login_page(public=_is_public(request.app),
                             name=_site_name(request)),
            status_code=200)
    return JSONResponse({"error": "unauthenticated"}, status_code=401)


async def _login(request: Request) -> Response:
    """``POST /__auth/login`` — ``{username?, password}``. Validate via the auth
    service and, on success, set the session cookie plus the readable
    ``awm_as`` twin. A locked username/IP answers 429 with ``Retry-After``."""
    gate: AuthGate = request.app.state.gate
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — accept form encoding as a fallback
        form = await request.form()
        data = {"password": form.get("password", ""),
                "username": form.get("username", "")}
    data = data or {}
    username = str(data.get("username") or "").strip() or None
    password = str(data.get("password") or "")
    verify_login = getattr(gate, "verify_login", None)
    if verify_login is None:
        token = await gate.verify_password(password)
        res = {"ok": True, "token": token, "sub": "operator"} if token else {"ok": False}
    else:
        res = await verify_login(username=username, password=password,
                                 client_ip=_client_ip(request))
    if res.get("locked"):
        retry = int(res.get("retry_after") or 60)
        return JSONResponse({"ok": False, "locked": True, "retry_after": retry},
                            status_code=429, headers={"Retry-After": str(retry)})
    if not res.get("ok"):
        return JSONResponse({"ok": False}, status_code=401)
    sub = str(res.get("sub") or "operator")
    resp = JSONResponse({"ok": True, "user": sub})
    ttl = int(await gate.session_ttl_seconds())
    samesite = _samesite(request.app)
    _set_session_cookie(resp, res["token"], ttl, samesite)
    _set_as_cookie(resp, sub, ttl, samesite)
    _clear_vault_cookies(resp)
    _clear_penpot_cookies(resp)
    return resp


def safe_local_path(raw: str | None, default: str = "/") -> str:
    """Coerce a caller-supplied redirect target into a *local* path.

    Anything that could leave this origin falls back to ``default``: a scheme, an
    authority (``//host``, and ``/\\host`` which some browsers normalise to one),
    a relative path, or a header-splitting character. Query and fragment are
    dropped — an autologin lands you on a *page*, and letting the caller append
    arbitrary query state to it widens the route for no current use.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return default
    if any(c in raw for c in ("\\", "\n", "\r", "\t", "\x00")):
        return default
    path = raw.split("#", 1)[0].split("?", 1)[0]
    return path or default


async def _auth_link(request: Request) -> Response:
    """``GET /__auth/link?p=<password>[&to=<local path>]`` — the tappable
    autologin the Discord password push carries.

    A password in a URL is only safe if the URL is never a page. So the *only*
    successful outcome here is a 302 to a clean local path with the session
    cookie already set: nothing is rendered under the address that carried the
    credential, the response is uncacheable, and no referrer leaks it onward. It
    cannot act as a bearer for an API call either — it mints a cookie and
    redirects, nothing else.

    A wrong or expired password sets no cookie and lands on the login form,
    without echoing the submitted value.
    """
    gate: AuthGate = request.app.state.gate
    dest = safe_local_path(request.query_params.get("to"))
    token = await gate.verify_password(request.query_params.get("p") or "")
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if not token:
        return RedirectResponse("/", status_code=302, headers=headers)
    resp = RedirectResponse(dest, status_code=302, headers=headers)
    ttl = int(await gate.session_ttl_seconds())
    _set_session_cookie(resp, token, ttl)
    _set_as_cookie(resp, "operator", ttl)
    return resp


def _clear_vault_cookies(resp: Response) -> None:
    """Drop Trilium's own cookies when the awm identity changes.

    They are not namespaced by person and they sit at ``path=/``, so after a
    second person signs in on the same browser profile the first person's
    session identifier would still be sent. The vault does not know it, mints a
    fresh session and overwrites — self-healing, but the already-rendered page
    holds a CSRF token bound to the old identifier, so the next write 403s and
    the client has to re-bootstrap. Identity changes at exactly two moments and
    both are here, so the window simply does not open.
    """
    for name in ("trilium.sid", "trilium-csrf"):
        resp.delete_cookie(name, path="/")


async def _logout(request: Request) -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    resp.delete_cookie(AS_COOKIE_NAME, path="/")
    _clear_vault_cookies(resp)
    _clear_penpot_cookies(resp)
    return resp


async def _whoami(request: Request) -> Response:
    ok, _, sub = await _authenticate_sub(request)
    if ok:
        return JSONResponse({"user": sub})
    return JSONResponse({"error": "unauthenticated"}, status_code=401)


async def _public_home(request: Request) -> Response:
    """``/`` on the public profile: the vault, or the login form first."""
    ok, _, _ = await _authenticate_sub(request)
    if not ok:
        return _deny(request)
    return RedirectResponse(PUBLIC_HOME, status_code=302)


async def _root(request: Request) -> Response:
    """Authenticated landing page at ``/`` — a dynamic index of ``/ui/*`` pages
    pulled from the gateway registry, tagged and filterable via ``store``."""
    ok, refreshed = await _authenticate(request)
    if not ok:
        return _deny(request)
    app = request.app
    services: list = []
    try:
        client: httpx.AsyncClient = app.state.client
        r = await client.get(app.state.http_up + "/hub/services")
        if r.status_code == 200:
            services = (r.json() or {}).get("services", [])
    except Exception as exc:  # noqa: BLE001 — degrade to an empty index
        log.debug("landing: could not fetch registry: %s", exc)
    dao = store.LandingDAO()
    page_names = [str(s.get("name", s.get("prefix", ""))) for s in services]
    tags_by_page = dao.tags_by_page(page_names)
    tag_counts = dao.all_tag_counts()
    selected = dao.selected_tags()
    display_names = dao.display_names(page_names)
    resp = HTMLResponse(
        pages.landing_page(
            services, tags_by_page, tag_counts, selected, _PEER_NAME,
            display_names=display_names,
        )
    )
    if refreshed:
        _set_session_cookie(resp, refreshed,
                            int(await app.state.gate.session_ttl_seconds()))
    return resp


async def _landing_add_tag(request: Request) -> Response:
    """``POST /__landing/tags`` — body ``{page, tag}``. Returns the page's
    updated tag list plus the refreshed global tag counts."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    page = str((data or {}).get("page") or "").strip()
    tag = str((data or {}).get("tag") or "").strip()
    if not page or not tag:
        return JSONResponse({"error": "page and tag are required"}, status_code=400)
    dao = store.LandingDAO()
    dao.add_tag(page, tag)
    return JSONResponse({
        "tags": dao.tags_for_page(page),
        "tag_counts": dao.all_tag_counts(),
    })


async def _landing_remove_tag(request: Request) -> Response:
    """``DELETE /__landing/tags`` — body ``{page, tag}``. Returns the page's
    updated tag list plus the refreshed global tag counts."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    page = str((data or {}).get("page") or "").strip()
    tag = str((data or {}).get("tag") or "").strip()
    if not page or not tag:
        return JSONResponse({"error": "page and tag are required"}, status_code=400)
    dao = store.LandingDAO()
    dao.remove_tag(page, tag)
    return JSONResponse({
        "tags": dao.tags_for_page(page),
        "tag_counts": dao.all_tag_counts(),
    })


async def _landing_select_filter(request: Request) -> Response:
    """``POST /__landing/filter`` — body ``{tag}``. Selects ``tag`` as a filter."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    tag = str((data or {}).get("tag") or "").strip()
    if not tag:
        return JSONResponse({"error": "tag is required"}, status_code=400)
    dao = store.LandingDAO()
    dao.select_tag(tag)
    return JSONResponse({"selected_tags": dao.selected_tags()})


async def _landing_deselect_filter(request: Request) -> Response:
    """``DELETE /__landing/filter`` — body ``{tag}``. Deselects ``tag``."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    tag = str((data or {}).get("tag") or "").strip()
    if not tag:
        return JSONResponse({"error": "tag is required"}, status_code=400)
    dao = store.LandingDAO()
    dao.deselect_tag(tag)
    return JSONResponse({"selected_tags": dao.selected_tags()})


async def _landing_set_name(request: Request) -> Response:
    """``POST /__landing/name`` — body ``{page, name}``. A blank/whitespace-only
    ``name`` clears the override, reverting the card to its technical label."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    page = str((data or {}).get("page") or "").strip()
    name = str((data or {}).get("name") or "").strip()
    if not page:
        return JSONResponse({"error": "page is required"}, status_code=400)
    dao = store.LandingDAO()
    if name:
        dao.set_display_name(page, name)
    else:
        dao.clear_display_name(page)
    return JSONResponse({"display_name": dao.display_name(page)})


# -- the vault --------------------------------------------------------------


def _vault_up(app) -> str | None:
    return getattr(app.state, "vault_http_up", None)


def _penpot_up(app) -> str | None:
    return getattr(app.state, "penpot_http_up", None)


async def _vault_bare(request: Request) -> Response:
    """``/trilium`` → ``/trilium/``, permanently.

    Not cosmetic, and the opposite of what this redirect used to say. Every
    reference in the vault's shell is relative, and they resolve against the
    document's *directory*: from ``/trilium/`` that is the mount and the edge
    strips it back off, from ``/trilium`` it is the site root and every one of
    them lands outside the vault. The page paints and then hangs — and Trilium's
    own hashchange parser, which wants the literal ``/#root``, ignores the back
    button either way.
    """
    return RedirectResponse(vault.SHELL, status_code=308)


async def _penpot_bare(request: Request) -> Response:
    """``/penpot`` → ``/penpot/``, permanently.

    Same reasoning as ``_vault_bare``, plus one Penpot has of its own: its
    client compares ``location.origin + location.pathname`` against its
    configured public URI, which always ends in a slash. Without the redirect
    the shell loads and renders Penpot's not-found page — which contains a
    login form, so it reads as a session problem and is not one.
    """
    return RedirectResponse(penpot.SHELL, status_code=308)


async def _vault_manifest(request: Request) -> Response:
    """The PWA manifest, ours rather than the vault's — see ``vault.manifest``."""
    return JSONResponse(vault.manifest(),
                        media_type="application/manifest+json")


async def _vault_logout(request: Request) -> Response:
    """The vault's logout button ends the awm session.

    There is no Trilium session to end — its own login is off — so without this
    the button would be a dead end inside the app. One login means one logout.
    """
    return RedirectResponse("/__auth/logout", status_code=302)


def _vault_unavailable(request: Request, reason: str) -> Response:
    """Why the vault is not answering, as a page rather than a bare 502.

    ``502 upstream gateway unreachable`` is what this used to be, and it reads
    as "awm is broken" when the true answer is almost always "the vault is
    still starting" — a cold child takes up to two minutes to bind. The status
    is 503 with a ``Retry-After`` so a browser and a monitor both do the right
    thing, and the page refreshes itself so nobody has to.
    """
    if request.method == "GET" and _wants_html(request):
        body = pages.vault_unavailable_page(reason)
        return HTMLResponse(body, status_code=503,
                            headers={"Retry-After": "5",
                                     "Cache-Control": "no-store"})
    return JSONResponse({"error": "vault unavailable", "reason": reason},
                        status_code=503, headers={"Retry-After": "5"})


def _penpot_unavailable(request: Request, reason: str) -> Response:
    """Penpot's own not-answering-yet response — same shape as
    ``_vault_unavailable`` and for the same reason (a cold container stack
    can take a while to bind), but inline rather than through ``pages.py``:
    that module is outside this change's file ownership, and its
    ``vault_unavailable_page`` is Trilium-branded copy that would misname the
    outage here.
    """
    if request.method == "GET" and _wants_html(request):
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="5">'
            "<title>penpot — starting</title></head><body>"
            "<h1>penpot</h1>"
            f"<p>Penpot is not answering yet: {reason}.</p>"
            "<p>This page retries every five seconds.</p>"
            "</body></html>"
        )
        return HTMLResponse(body, status_code=503,
                            headers={"Retry-After": "5",
                                     "Cache-Control": "no-store"})
    return JSONResponse({"error": "penpot unavailable", "reason": reason},
                        status_code=503, headers={"Retry-After": "5"})


#: Penpot RPC paths, *inside the mount*. A 401 from one of these is the SPA
#: telling us the cookie it was given no longer works — the one moment the edge
#: can see a stale Penpot session, since the shell document itself is static
#: nginx output that says nothing about who is asking.
_PENPOT_RPC_PREFIX = "/api/rpc/"


def _penpot_bridge_kind(request: Request, path: str) -> str | None:
    """``"shell"``, ``"rpc"`` or ``None`` — where the sign-in bridge applies.

    Only the shell document, not every asset: a page load is one document and a
    hundred files, and putting an RPC to the auth service in front of each of
    those would be a round trip per sprite. Penpot routes on the URL *fragment*,
    so ``/penpot/`` is the only document there is and once per page load is
    exactly right.
    """
    if request.method == "GET" and path == penpot.SHELL:
        return "shell"
    if penpot.upstream_path(path).startswith(_PENPOT_RPC_PREFIX):
        return "rpc"
    return None


async def _http_proxy(request: Request) -> Response:
    app = request.app
    public = _is_public(app)
    path = request.url.path
    raw = _raw_target(request.scope, path)
    # Before the upstream branch, or the gateway leg keeps the hole.
    if _re_segments(raw, path):
        return _not_found()
    if public and policy.classify(path) is policy.Verdict.DENY:
        return _not_found()
    # Penpot's own credential commands, refused here as well as in the policy
    # door: a mesh node's edge runs no profile and consults no policy, so the
    # line above is not reached there at all.
    if _penpot_up(app) and penpot.refused(path):
        return _not_found()
    ok, refreshed, sub = await _authenticate_sub(request)
    if not ok:
        return _deny(request)
    if public and not policy.allows(path, sub):
        return _not_found()
    # The vault and Penpot are the upstreams on this listener that are not the
    # gateway. Which upstream is decided here and nowhere else, from a path
    # the caller cannot use to name anything but one of these two apps. The
    # vault is checked first — unchanged from before Penpot existed — so a
    # host running both keeps the vault's pre-existing behaviour; see
    # penpot.py's module docstring for the root-path collision that follows.
    up = app.state.http_up
    vault_up = _vault_up(app)
    penpot_up = _penpot_up(app)
    bridge: str | None = None
    if vault_up and vault.owns(path):
        if not public and sub in (PEER_SUB, "operator"):
            # The mesh edge runs no allow-list, so the check `policy.allows`
            # would have made on the public profile is made here instead.
            return _not_found()
        inner = vault.upstream_raw_path(raw)
        if inner is None:
            # The mount is in the decoded path but not in the bytes.
            return _not_found()
        up, raw = vault_up, inner
    elif penpot_up and penpot.owns(path):
        if not public and sub in (PEER_SUB, "operator"):
            return _not_found()
        inner = penpot.upstream_raw_path(raw)
        if inner is None:
            return _not_found()
        up, raw = penpot_up, inner
        bridge = _penpot_bridge_kind(request, path)
    client: httpx.AsyncClient = app.state.client
    url = _upstream_url(up, raw, request.scope.get("query_string") or b"")
    body = await request.body()
    upstream_req = client.build_request(
        request.method, url, headers=_req_headers(request, sub), content=body,
    )
    try:
        resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        if up is vault_up:
            return _vault_unavailable(request, "not listening yet")
        if up is penpot_up:
            return _penpot_unavailable(request, "not listening yet")
        return Response("upstream gateway unreachable", status_code=502)
    out = StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        background=BackgroundTask(resp.aclose),
    )
    # Assign the raw list rather than passing ``headers=dict(...)``: a dict keeps
    # only the last of a repeated key, and ``Set-Cookie`` is repeated whenever an
    # upstream sets more than one cookie in a response. Collapsing those loses
    # every cookie but the last, which is invisible until a wrapped app's session
    # simply fails to establish. ``set_cookie`` below appends to this same list.
    out.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1")) for k, v in _resp_headers(resp)
    ]
    if refreshed:
        _set_session_cookie(out, refreshed,
                            int(await app.state.gate.session_ttl_seconds()),
                            _samesite(app))
    if bridge and sub:
        await _bridge_penpot_session(request, out, bridge, sub,
                                     resp.status_code)
    return out


async def _bridge_penpot_session(request: Request, out: Response, kind: str,
                                 sub: str, status: int) -> None:
    """Give this browser the Penpot session awm holds for ``sub``.

    Two moments, and only two. On the **shell** document, before the SPA boots,
    so it comes up already signed in — the cookie is replaced only when it
    differs from the one awm holds, which is what makes a nightly rotation
    invisible: the rotation ends the browser's session and leaves awm's intact,
    so the very next page load carries the survivor.

    On an **RPC 401**, which is the SPA reporting that the cookie it was given
    has stopped working. That is the case a page load cannot fix on its own,
    because the shell is static nginx output and 200s for anyone. Note that
    ``get-profile`` is *not* one of the commands that says so: with a dead
    cookie Penpot answers it 200 with the anonymous profile, so anything asking
    "is this session still alive?" has to ask a command that genuinely requires
    authentication (``get-teams`` does). Verified against a live stack. The re-login
    is conditional on the presented cookie still being the one awm has cached
    (see ``AuthGate.penpot_session``), so a page's worth of simultaneous 401s
    costs one login rather than one each.

    Everything here degrades to doing nothing: no credential recorded, an auth
    service that is down, a machine bearer. Penpot then shows its own login
    screen, which is exactly what it did before this bridge existed.
    """
    if sub in (PEER_SUB, "operator"):
        return
    presented = request.cookies.get(penpot.COOKIE_NAME)
    gate: AuthGate = request.app.state.gate
    # Same shape as the ``verify_login`` lookup in ``_login``: a gate that
    # predates this capability simply does not bridge, rather than 500ing every
    # Penpot page on a host whose edge has not caught up.
    if getattr(gate, "penpot_session", None) is None:
        return
    if kind == "rpc":
        if status != 401:
            return
        token = await gate.penpot_session(sub, stale_token=presented)
    else:
        token = await gate.penpot_session(sub)
    if not token or token == presented:
        return
    _set_penpot_cookie(out, token,
                       int(await gate.session_ttl_seconds()),
                       _samesite(request.app))
    # Belt and braces on a response that now carries one person's identity.
    # Penpot's own nginx already sends no-store for the shell and Cloudflare
    # does not cache a response with Set-Cookie — but a cached copy of this
    # response would hand the next visitor somebody else's design files, and
    # that failure is bad enough that it should not rest on two other parties
    # continuing to behave.
    out.headers["cache-control"] = "no-store"


async def _ca(request: Request) -> Response:
    """Serve the local root CA so a device can install + trust it once, clearing
    ERR_CERT_AUTHORITY_INVALID. Sent as a downloadable
    ``application/x-x509-ca-cert`` so Android/iOS offer to install it."""
    ca_path: str = request.app.state.ca_path
    try:
        body = Path(ca_path).read_bytes()
    except OSError:
        return Response("CA not available", status_code=404)
    return Response(
        body,
        media_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": 'attachment; filename="awm-ca.crt"'},
    )


async def _ws_proxy(ws: WebSocket) -> None:
    """Bridge a browser WebSocket to the same path on the loopback gateway,
    pumping text+binary frames both directions until either side closes."""
    app = ws.app
    path = ws.url.path
    raw = _raw_target(ws.scope, path)
    if _re_segments(raw, path):
        await ws.close(code=1008)
        return
    query = ws.scope.get("query_string") or b""
    up = app.state.ws_up
    vault_ws = getattr(app.state, "vault_ws_up", None)
    is_vault = bool(vault_ws) and vault.owns(path)
    penpot_ws = getattr(app.state, "penpot_ws_up", None)
    # Vault takes precedence on a path both would claim — same ordering as the
    # HTTP leg, and for the same reason (see penpot.py's collision note): the
    # pre-existing feature's behaviour must not shift under it.
    is_penpot = (bool(penpot_ws) and not is_vault
                 and penpot.owns(path))
    if is_vault:
        # The client derives its socket URL from the page's own pathname, so a
        # shell served at /trilium/ opens its socket there. Same rewrite as the
        # HTTP leg, or the vault's live updates never arrive and nothing says so.
        inner = vault.upstream_raw_path(raw)
        if inner is None:
            await ws.close(code=1008)
            return
        up, raw = vault_ws, inner
    elif is_penpot:
        # Penpot's collab socket (/ws/notifications) is what keeps a shared
        # board's live edits in sync — the same "silent wrong upstream" hazard
        # as the vault's socket, and the whole reason this leg exists at all.
        inner = penpot.upstream_raw_path(raw)
        if inner is None:
            await ws.close(code=1008)
            return
        up, raw = penpot_ws, inner
    # ``websockets.connect`` parses the URI without decoding its path, so the
    # bytes reach the upstream as they arrived — the same contract the HTTP leg
    # gets from ``copy_with(raw_path=…)``.
    up_url = up + (raw + (b"?" + query if query else b"")).decode("ascii")

    # Edge auth: Starlette HTTP handling never sees a WS scope, so the guard is
    # enforced here, before accept(). A browser sends the session cookie on the
    # WS handshake (same-origin); a peer/client sends a bearer. No cookie
    # refresh on a WS (it is not an HTTP response).
    gate: AuthGate = app.state.gate
    public = _is_public(app)
    if public and policy.classify(ws.url.path) is policy.Verdict.DENY:
        await ws.close(code=1008)
        return
    ok, _, sub = _unpack(await gate.authenticate(
        cookie=ws.cookies.get(COOKIE_NAME),
        bearer=bearer_of(ws.headers.get("authorization")),
    ))
    if not ok or (public and not policy.allows(ws.url.path, sub)):
        await ws.close(code=1008)  # policy violation
        return
    if (is_vault or is_penpot) and sub in (PEER_SUB, "operator"):
        await ws.close(code=1008)
        return

    # Forward cookies and the verified identity so the gateway sees the real
    # caller. ``origin`` and the forwarded-* hints matter only for a
    # reverse-proxied third party: an upstream that origin-checks its WS
    # upgrades (an allowlist of the exact scheme+host+port a browser may
    # connect from) rejects every handshake if the header never arrives. The
    # gateway ignores all but X-Awm-As.
    fwd = {}
    for k in ("cookie", "authorization", "origin"):
        v = ws.headers.get(k)
        if v:
            fwd[k] = v
    fwd["X-Awm-As"] = _as_header(sub)
    override = _origin_override(app)
    if override and "origin" in fwd:
        fwd["origin"] = override
    fwd["X-Forwarded-Proto"] = "https"
    host = ws.headers.get("host")
    if host:
        fwd["X-Forwarded-Host"] = host

    # Subprotocols are negotiated end to end rather than dropped: a browser that
    # asks for one and gets a handshake response without it fails the connection
    # per RFC 6455. No awm service uses them, so `or None` keeps the gateway
    # front's behaviour byte-identical.
    subprotocols = list(ws.scope.get("subprotocols") or [])

    try:
        upstream = await websockets.connect(
            up_url, additional_headers=fwd, max_size=None, open_timeout=10,
            subprotocols=subprotocols or None,
        )
    except Exception as exc:  # noqa: BLE001 — upstream refused / bad path
        # Path only, never the query string — see the log_level note in serve().
        log.debug("ws upstream connect failed for %s: %s", ws.url.path, exc)
        # Reject the handshake outright rather than accept-then-close, so the
        # client sees a clean failure instead of a socket that opens and dies.
        await ws.close(code=1011)
        return
    await ws.accept(subprotocol=getattr(upstream, "subprotocol", None))

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except WebSocketDisconnect:
            pass

    async def upstream_to_client() -> None:
        try:
            async for data in upstream:
                if isinstance(data, (bytes, bytearray)):
                    await ws.send_bytes(bytes(data))
                else:
                    await ws.send_text(data)
        except Exception:  # noqa: BLE001 — upstream closed
            pass

    a = asyncio.ensure_future(client_to_upstream())
    b = asyncio.ensure_future(upstream_to_client())
    try:
        await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (a, b):
            task.cancel()
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def _origin_of(url: str) -> str:
    """``scheme://host[:port]`` of ``url`` — an RFC 6454 origin, no path."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


#: An extra endpoint a wrapping front adds: ``(path, methods, handler)``.
ExtraRoute = tuple[str, Sequence[str], Callable[[Request], Awaitable[Response]]]


def _gated(
    handler: Callable[[Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    """Wrap an extra route in the edge session check the catch-all proxy applies.

    Same deny shape (login page for a browser GET, 401 otherwise) and the same
    sliding-cookie refresh, so an added endpoint is indistinguishable from a
    proxied path as far as auth is concerned.
    """
    async def _wrapped(request: Request) -> Response:
        ok, refreshed = await _authenticate(request)
        if not ok:
            return _deny(request)
        resp = await handler(request)
        if refreshed:
            _set_session_cookie(
                resp, refreshed,
                int(await request.app.state.gate.session_ttl_seconds()),
                _samesite(request.app),
            )
        return resp

    return _wrapped


def build_app(upstream: str, ca_path: str, *, landing: bool = True,
              extra_routes: Sequence[ExtraRoute] | None = None,
              rewrite_origin: bool = False,
              profile: str | None = None,
              vault_upstream: str | None = None,
              penpot_upstream: str | None = None) -> Starlette:
    """Assemble the front. ``landing=False`` drops the awm index page at ``/``.

    ``profile="public"`` builds the internet-facing door: no CA download, no
    autologin link, no landing page (``/`` sends a signed-in browser to the
    notes page), ``SameSite=Strict`` cookies, and every proxied path checked
    against :mod:`awm.httpsfront.policy` — a path off the list is 404 whether
    or not the caller is signed in.

    The landing page is right for the gateway front, whose ``/`` has nothing
    else to serve. It is wrong for a front that sits in front of a *single*
    app — a wrapped app's SPA lives at ``/``, and the index route would shadow
    it. Turning it off lets ``/`` fall through to the catch-all proxy, which is
    what makes this module reusable for any upstream rather than the gateway
    alone. Default keeps the gateway front's behaviour byte-identical.

    ``extra_routes`` are ``(path, methods, handler)`` triples for endpoints the
    upstream cannot serve itself — a sign-in shim that has to shell out to the
    wrapped binary, say. They are registered after the ``/__auth/*`` routes, so
    they cannot shadow the edge's own auth surface, and **this function gates
    them** with the same check the catch-all proxy applies, including the
    sliding cookie refresh. Handlers are therefore plain endpoints that may
    assume an authenticated caller: the gate is not something a caller can
    forget, because the whole reason such a route exists is that it does
    something privileged on the upstream's behalf.

    ``vault_upstream`` adds the shared knowledge base as a *second* upstream on
    this same listener, mounted at :data:`vault.SHELL`. It is what makes one
    origin, one session and one login cover both awm and the vault. Off by
    default, so every existing caller is unchanged.

    ``penpot_upstream`` adds Penpot the same way, mounted at
    :data:`penpot.SHELL`. Both mounts are prefixes, so the two apps and the
    edge's own surface are disjoint by construction and all three can run on
    one listener. Unlike the vault, Penpot does not claim ``/logout``: its own
    logout ends a real Penpot session and stays Penpot's, where the vault's own
    login is off entirely and its logout button would otherwise be a dead end.

    Penpot additionally requires ``PENPOT_PUBLIC_URI`` on its containers to
    carry this same mount — the edge cannot enforce that from here, and a
    disagreement renders Penpot's not-found page on every route. See
    ``penpot.py``'s module docstring.

    ``rewrite_origin=True`` replaces a present ``Origin`` with the upstream's
    own scheme+authority on both the HTTP and the WebSocket path. Two wrapped
    apps want opposite things here. One origin-checks its WebSocket upgrades
    against an allowlist of the exact browser origins it expects, and needs the
    real header (``claude-science``). The other compares ``Origin`` against
    ``Host`` and demands they match — and since ``Host`` is dropped so httpx
    derives it from the upstream URL, the browser's real ``Origin`` can never
    match it, so every request 403s (``dsh``). Default off: the gateway front
    and every existing caller keep byte-identical behaviour.
    """
    http_up = upstream.rstrip("/")
    # http://… → ws://… ,  https://… → wss://…
    ws_up = "ws" + http_up[len("http"):]

    public = profile == PUBLIC_PROFILE
    routes = []
    if not public:
        # Public (no auth): CA download so a device can install the root once.
        routes.append(Route("/ca.crt", _ca, methods=["GET"]))
        routes.append(Route("/ca.pem", _ca, methods=["GET"]))
    # Auth endpoints — handled by the edge itself, never proxied.
    routes.append(Route("/__auth/login", _login, methods=["POST"]))
    if not public:
        # Autologin from the Discord password push — validates and 302s; see
        # _auth_link for why it may never render a page.
        routes.append(Route("/__auth/link", _auth_link, methods=["GET"]))
    routes.append(Route("/__auth/logout", _logout, methods=["POST", "GET"]))
    routes.append(Route("/__auth/whoami", _whoami, methods=["GET"]))
    if public:
        routes.append(Route("/", _public_home, methods=["GET"]))
    elif landing:
        # Authenticated landing page (dynamic index of /ui/* pages).
        routes.append(Route("/", _root, methods=["GET"]))
    if landing and not public:
        # Tag/filter endpoints backing the landing page's tagging UI.
        routes.append(Route("/__landing/tags", _gated(_landing_add_tag), methods=["POST"]))
        routes.append(Route("/__landing/tags", _gated(_landing_remove_tag), methods=["DELETE"]))
        routes.append(Route("/__landing/filter", _gated(_landing_select_filter), methods=["POST"]))
        routes.append(Route("/__landing/filter", _gated(_landing_deselect_filter), methods=["DELETE"]))
        routes.append(Route("/__landing/name", _gated(_landing_set_name), methods=["POST"]))
    if vault_upstream:
        # Before the catch-all, and after /__auth/*: these three are the edge's
        # own answers on paths the vault would otherwise be asked for.
        routes.append(Route(vault.SHELL_BARE, _vault_bare, methods=["GET"]))
        routes.append(Route(vault.MANIFEST, _gated(_vault_manifest), methods=["GET"]))
        routes.append(Route(vault.LOGOUT, _vault_logout, methods=["GET", "POST"]))
    if penpot_upstream:
        # Only the bare-mount redirect — no manifest override (Penpot ships
        # none to fix up) and no /logout hijack (Penpot's own logout is
        # meaningful and stays Penpot's; see the docstring above).
        routes.append(Route(penpot.SHELL_BARE, _penpot_bare, methods=["GET"]))
    for path, methods, handler in (extra_routes or ()):
        routes.append(Route(path, _gated(handler), methods=list(methods)))
    routes += [
        # Everything else is auth-gated inside the handler, then proxied.
        WebSocketRoute("/{path:path}", _ws_proxy),
        Route("/{path:path}", _http_proxy, methods=_ALL_METHODS),
    ]
    # Lifespan (Starlette 1.3+ removed the @app.on_event decorator): own the
    # shared upstream httpx client for the server's lifetime.
    @asynccontextmanager
    async def _lifespan(app_: Starlette):
        app_.state.client = httpx.AsyncClient(timeout=None, follow_redirects=False)
        try:
            yield
        finally:
            await app_.state.client.aclose()

    app = Starlette(routes=routes, lifespan=_lifespan)
    app.state.http_up = http_up
    app.state.ws_up = ws_up
    app.state.ca_path = ca_path
    # Derived from the upstream URL rather than taken as a string, so the
    # rewritten Origin is by construction the one httpx will also put in Host.
    app.state.origin_override = (
        _origin_of(http_up) if rewrite_origin else None
    )
    app.state.gate = AuthGate()
    app.state.profile = profile
    if vault_upstream:
        v = vault_upstream.rstrip("/")
        app.state.vault_http_up = v
        app.state.vault_ws_up = "ws" + v[len("http"):]
    else:
        app.state.vault_http_up = None
        app.state.vault_ws_up = None
    if penpot_upstream:
        p = penpot_upstream.rstrip("/")
        app.state.penpot_http_up = p
        app.state.penpot_ws_up = "ws" + p[len("http"):]
    else:
        app.state.penpot_http_up = None
        app.state.penpot_ws_up = None
    return app


def serve(*, port: int, cert: str, key: str, ca: str, upstream: str,
          landing: bool = True,
          extra_routes: Sequence[ExtraRoute] | None = None,
          rewrite_origin: bool = False,
          profile: str | None = None,
          tls: bool = True,
          vault_upstream: str | None = None,
          penpot_upstream: str | None = None) -> None:
    """Bind ``0.0.0.0:port`` with TLS and reverse-proxy to ``upstream`` forever
    (blocks). Designed to run in a daemon thread from the hub adapter.

    ``tls=False`` binds plain HTTP on ``127.0.0.1:port`` instead, trusting
    ``X-Forwarded-*`` from loopback only — the shape for a TLS-terminating
    nginx in front (the public host). ``cert``/``key`` are unused then.

    ``upstream``, ``landing`` and ``extra_routes`` are what make this reusable
    beyond the gateway: the ``claude-science`` service calls it against its own
    loopback binary with ``landing=False`` to get TLS, the shared CA, and the
    ``awm_session`` edge auth without duplicating any of it, and adds one gated
    sign-in route the wrapped binary cannot serve itself.
    """
    app = build_app(upstream, ca, landing=landing, extra_routes=extra_routes,
                    rewrite_origin=rewrite_origin, profile=profile,
                    vault_upstream=vault_upstream,
                    penpot_upstream=penpot_upstream)
    bind: dict = (
        {"host": "0.0.0.0", "ssl_certfile": cert, "ssl_keyfile": key}
        if tls else
        {"host": "127.0.0.1", "proxy_headers": True,
         "forwarded_allow_ips": "127.0.0.1"}
    )
    config = uvicorn.Config(
        app,
        port=port,
        **bind,
        # SECURITY, not tidiness: uvicorn writes access records — including the
        # full query string — to `uvicorn.access` at INFO. `GET /__auth/link?p=…`
        # carries a live login password, so raising this to "info" or "debug" to
        # chase an unrelated problem would start writing passwords to disk.
        # Pinned by tests/test_no_access_logging.py.
        log_level="warning",
        ws="websockets",
        timeout_keep_alive=75,
        # Off the main thread uvicorn skips signal handlers automatically.
    )
    server = uvicorn.Server(config)
    log.info("front listening on %s:%d → %s (tls %s, profile %s)",
             bind["host"], port, upstream, "on" if tls else "off",
             profile or "default")
    server.run()
