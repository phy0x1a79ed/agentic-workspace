"""The whole-gateway HTTPS front — TLS for the awm gateway and its neighbours.

The awm gateway binds **loopback plain HTTP** (``127.0.0.1:7819``) with no auth,
by design. But powerful browser APIs — ``getUserMedia`` for the notes-page
dictation, clipboard, etc. — need a *secure context*, which off-localhost means
HTTPS. So this listener terminates TLS on ``0.0.0.0:<port>`` and transparently
reverse-proxies **every** request — HTTP and WebSocket alike — to the loopback
gateway, so the entire awm surface (pages at ``/p/<plugin>/<page>/``, ``/invoke``,
service mounts at ``/s/<service>/``, the hub control plane, …) is reachable over
one HTTPS origin.

Because every awm page makes *same-origin* relative calls, fronting the gateway
wholesale is what makes those calls Just Work under HTTPS — there is no
per-path allowlist to keep in sync.

The TLS relay, the edge session and the raw-path contract are
:mod:`awm.tlsfront.proxy`'s. This module adds what only the gateway's front
carries: the landing page at ``/``, the vault, slice, tether and board mounts,
and the public profile (``AWM_EDGE_PROFILE=public``), which narrows the door to
the allow-list in :mod:`awm.httpsfront.policy` and, with ``AWM_EDGE_TLS=0``,
serves plain HTTP on loopback for a TLS-terminating nginx in front.

Runs in a daemon thread launched from the hub adapter's ``on_start``; when the
gateway drains/respawns this service, the process exits and the listener dies
with it (one supervised lifetime, exactly like ``mic``).
"""

from __future__ import annotations

import json
import logging
import socket
import time
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.websockets import WebSocket

from awm import gatewayclient
from awm.tlsfront.auth import is_machine_sub
from awm.tlsfront.proxy import build_app as build_front
from awm.tlsfront.proxy import (
    CALLER_HEADERS,
    HOP,
    as_header,
    authenticate_sub,
    authenticate_ws,
    bridge,
    client_headers,
    consumed_edge_token,
    deny,
    forward,
    gated,
    http_shape,
    not_found,
    query_of,
    raw_target,
    re_segments,
    refresh_cookie,
    req_headers,
    samesite,
    serve_app,
    streamed,
    upstream_unreachable,
    upstream_url,
    wants_html,
    ws_headers,
    ws_url,
)

from awm.httpsfront import board, pages, policy, slices, store, tether, vault

log = logging.getLogger("awm.httpsfront.proxy")

_PEER_NAME = socket.gethostname().split(".")[0].title()

#: The header the gateway knows a service's own calls by.
SERVICE_TOKEN_HEADER = "X-Awm-Service-Token"

PUBLIC_PROFILE = "public"
# Where a signed-in browser lands on the public profile. The vault, because
# the vault is what this host is for. Kept as a redirect from `/` rather than
# serving the shell there: `/` is the one path the sign-in form reloads, and a
# `/` that fell through to the catch-all would proxy the gateway's own page
# index — an enumeration of the whole internal surface — to the internet.
PUBLIC_HOME = vault.SHELL

#: Trilium's own cookies, dropped whenever the awm identity changes.
#:
#: They are not namespaced by person and they sit at ``path=/``, so after a
#: second person signs in on the same browser profile the first person's
#: session identifier would still be sent. The vault does not know it, mints a
#: fresh session and overwrites — self-healing, but the already-rendered page
#: holds a CSRF token bound to the old identifier, so the next write 403s and
#: the client has to re-bootstrap. Identity changes at sign-in and sign-out,
#: and the library clears these at both, so the window simply does not open.
VAULT_COOKIES = ("trilium.sid", "trilium-csrf")


def _is_public(app) -> bool:
    return getattr(app.state, "profile", None) == PUBLIC_PROFILE


async def _public_home(request: Request) -> Response:
    """``/`` on the public profile: the vault, or the login form first."""
    ok, _, _ = await authenticate_sub(request)
    if not ok:
        return deny(request)
    return RedirectResponse(PUBLIC_HOME, status_code=302)


async def _root(request: Request) -> Response:
    """Authenticated landing page at ``/`` — an index of the pages the
    gateway's loaded plugins serve, tagged and filterable via ``store``."""
    ok, refreshed, _ = await authenticate_sub(request)
    if not ok:
        return deny(request)
    app = request.app
    entries: list = []
    try:
        client: httpx.AsyncClient = app.state.client
        r = await client.get(app.state.http_up + "/hub/plugins")
        if r.status_code == 200:
            entries = pages.page_entries(r.json())
    except Exception as exc:  # noqa: BLE001 — degrade to an empty index
        log.debug("landing: could not fetch the plugin snapshot: %s", exc)
    dao = store.LandingDAO()
    page_names = [e["name"] for e in entries]
    resp = HTMLResponse(
        pages.landing_page(
            entries, dao.tags_by_page(page_names), dao.all_tag_counts(),
            dao.selected_tags(), _PEER_NAME,
            display_names=dao.display_names(page_names),
        )
    )
    await refresh_cookie(app, resp, refreshed)
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


def _tether_up(app) -> str | None:
    return getattr(app.state, "tether_http_up", None)


def _board_up(app) -> str | None:
    return getattr(app.state, "board_http_up", None)


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
    if request.method == "GET" and wants_html(request):
        body = pages.vault_unavailable_page(reason)
        return HTMLResponse(body, status_code=503,
                            headers={"Retry-After": "5",
                                     "Cache-Control": "no-store"})
    return JSONResponse({"error": "vault unavailable", "reason": reason},
                        status_code=503, headers={"Retry-After": "5"})


async def _public_invoke(request: Request, sub: str | None,
                         refreshed: str | None) -> Response:
    """``/invoke`` on the public profile: the trilium page's read verbs only.

    The body is read under a cap and checked before anything is forwarded,
    then sent as read, so what was checked is what the gateway runs.
    """
    content = await _capped_body(request, policy.INVOKE_BODY_CAP)
    if content is None or not policy.invoke_allows(content):
        return not_found()
    raw = raw_target(request.scope, request.url.path)
    return await forward(request, request.app.state.http_up, raw, sub, refreshed,
                         content=content)


async def _http_proxy(request: Request) -> Response:
    app = request.app
    public = _is_public(app)
    path = request.url.path
    raw = raw_target(request.scope, path)
    # Before the upstream branch, or the gateway leg keeps the hole.
    if re_segments(raw, path):
        return not_found()
    verdict = policy.classify(path) if public else None
    if verdict is policy.Verdict.DENY:
        return not_found()
    # `/invoke` is called with POST and nothing else, so any other method is a
    # path off the list, signed in or not.
    if verdict is policy.Verdict.INVOKE and request.method != "POST":
        return not_found()
    # The slice mount, answered before authentication rather than after it.
    # This is the one path on this listener a caller reaches without a session,
    # and it stays narrow because the token has to name a slice the trilium
    # service confirms, and because Trilium's own mask refuses every note
    # outside that slice.
    vault_up = _vault_up(app)
    if vault_up and slices.refused(path):
        # Before the branch below, and not left to the policy door: this node may run no profile.
        return not_found()
    if vault_up and slices.owns(path):
        return await _slice_proxy(request, path, raw, vault_up)
    # The tether mount, answered before authentication for the same structural
    # reason as the slice above and a different human one: the caller is the
    # person being helped, on their own machine, and an awm account is exactly
    # what they do not have.
    #
    # The name is claimed on every node and wired on few, and both branches run
    # unconditionally. The policy door classifies this mount without consulting
    # any upstream, so a path it declares reachable with no session must never
    # then be asked for one — a 401 there would contradict the verdict and tell
    # an anonymous caller the path is special. Whether a relay is configured
    # decides 404-versus-proxy, never who owns the name, and nothing else in
    # awm answers under /tether. Here rather than only in the policy door
    # because a mesh node's edge runs no profile at all.
    tether_up = _tether_up(app)
    if tether.refused(path):
        return not_found()
    if tether.owns(path):
        return (await _tether_proxy(request, raw, tether_up)
                if tether_up else not_found())
    # The board mount, answered before authentication because the board
    # authenticates its own parties and the edge must not stand in front of
    # that with a login or a node token. The whole mount is claimed on every
    # node, wired or not, so a path under it never reaches the gateway.
    if board.in_mount(path):
        board_up = _board_up(app)
        if not board_up or not board.allows(request.method, path):
            return not_found()
        return await _board_proxy(request, raw, board_up)
    ok, refreshed, sub = await authenticate_sub(request, http_shape(request, path, raw))
    if not ok:
        return deny(request)
    if public and not policy.allows(path, sub):
        return not_found()
    if verdict is policy.Verdict.INVOKE:
        return await _public_invoke(request, sub, refreshed)
    # The vault is the upstream on this listener that is not the gateway.
    # Which upstream is decided here and nowhere else, from a path the caller
    # cannot use to name anything but the vault.
    if vault_up and vault.owns(path):
        if not public and is_machine_sub(sub):
            # The mesh edge runs no allow-list, so the check `policy.allows`
            # would have made on the public profile is made here instead.
            return not_found()
        inner = vault.upstream_raw_path(raw)
        if inner is None:
            # The mount is in the decoded path but not in the bytes.
            return not_found()
        return await forward(
            request, vault_up, inner, sub, refreshed,
            unreachable=lambda r: _vault_unavailable(r, "not listening yet"))
    return await forward(request, app.state.http_up, raw, sub, refreshed,
                         unreachable=upstream_unreachable)


async def _tether_proxy(request: Request, raw: bytes, up: str) -> Response:
    """Forward one request to the tether relay. No session, and no opinion.

    Everything this mount decides, it decides in :mod:`awm.httpsfront.tether`
    by the shape of the path. What is left here is transport — and the two
    headers that matter on the way through:

    ``authorization`` rides untouched, because the relay's own bearer is the
    gate on ``/issue`` and ``/status``. The edge deliberately does not try to
    read it: it is a credential for the upstream, not for awm, and an edge that
    checked it would be a second place to get that check wrong.

    ``X-Awm-As`` is removed rather than defaulted. Every other leg stamps a
    verified identity there; this one has none, and sending ``user:operator``
    for an anonymous caller would be the edge asserting something it did not
    check. The client's address still arrives in the forwarded headers, which
    is what the relay's per-address budget is counted on — and why the owner's
    client claims a ticket on this plain leg before it opens a socket, where no
    such header survives.
    """
    inner = tether.upstream_raw_path(raw)
    if inner is None:
        # The mount is in the decoded path but not in the bytes.
        return not_found()
    headers = req_headers(request)
    headers.pop("X-Awm-As", None)

    client: httpx.AsyncClient = request.app.state.client
    url = upstream_url(up, inner, request.scope.get("query_string") or b"")
    upstream_req = client.build_request(
        request.method, url, headers=headers, content=await request.body(),
    )
    try:
        resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        # The same answer the relay gives for everything it declines, so a
        # relay that is down and a slot that never existed are one reply.
        return not_found()
    out = streamed(resp)
    return out


def _board_headers(request: Request, *, drop_auth: bool = False) -> dict[str, str]:
    """The request headers the board is sent: the caller's, minus every claim of identity.

    ``Authorization`` is the board's own party bearer and rides untouched, unless
    ``drop_auth`` says it is a device token, which is removed so the board never
    holds a credential that is live elsewhere. Every ``X-Awm-*`` header goes, not only the four the
    gateway reads, and so does the edge's session cookie. ``Content-Length`` is
    dropped because the body is re-read under a cap and httpx restates it.
    ``Last-Event-ID`` and everything else is forwarded as sent.
    """
    hdrs = {}
    for key, value in request.headers.items():
        low = key.lower()
        if (low in HOP or low in CALLER_HEADERS or low.startswith("x-awm-")
                or low.startswith("x-forwarded-") or low in ("cookie", "content-length")):
            continue
        hdrs[key] = value
    if drop_auth:
        hdrs.pop("authorization", None)
    hdrs["X-Forwarded-Proto"] = "https"
    host = request.headers.get("host")
    if host:
        hdrs["X-Forwarded-Host"] = host
    if request.client:
        hdrs["X-Forwarded-For"] = request.client.host
    return hdrs


async def _capped_body(request: Request, cap: int) -> bytes | None:
    """The request body, or ``None`` once it exceeds ``cap`` bytes.

    Checked against the declared length first, then while reading, so a chunked
    body with no length is stopped as well.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _relay(resp: httpx.Response):
    """The upstream body as it arrives, closing the upstream however the client leaves.

    A background task would not run when the client drops mid-stream, and a
    board stream is open until somebody does exactly that.
    """
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
    except httpx.TransportError:
        # The board went away mid-stream: end the body, as a closed socket would.
        pass
    finally:
        await resp.aclose()


#: The board leg's own connection pool. Every open ``/board/stream`` holds a
#: connection for as long as its party stays, and the shared client's pool is
#: also what every other leg of the edge waits on, so board streams must not be
#: able to fill it. Read is unbounded (a stream is quiet for up to 30 s between
#: heartbeats); the pool wait is short so exhaustion fails fast instead of
#: queueing.
BOARD_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=16)
BOARD_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=2.0)


def _board_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(limits=BOARD_LIMITS, timeout=BOARD_TIMEOUT,
                             follow_redirects=False)


async def _board_proxy(request: Request, raw: bytes, up: str) -> Response:
    """Forward one request to the board. No session, and no opinion.

    What the mount allows is decided in :mod:`awm.httpsfront.board` by the shape
    of the method and path. What is left is transport, and the headers that
    matter on the way through (see :func:`_board_headers`).

    The board's answers pass through as sent: its 404 for every refusal, its
    409 for a held claim, its 400 for a bad body. The stream is relayed chunk
    by chunk with no read timeout, because the board speaks on it only every
    30 s. It uses the board's own client (see :data:`BOARD_LIMITS`), so a crowd
    of open streams can exhaust that pool and nothing else on the edge; a
    request that cannot get a slot is answered 503 at once.

    ``Authorization`` is checked as a set: more than one header is refused,
    because the device-token test and the forwarded value would otherwise be able
    to read different ones.
    """
    inner = board.upstream_raw_path(raw)
    if inner is None:
        return not_found()
    auths = request.headers.getlist("authorization")
    if len(auths) > 1:
        return not_found()
    drop_auth = bool(auths) and consumed_edge_token(auths[0])
    body = await _capped_body(request, board.body_cap(request.method, request.url.path))
    if body is None:
        return Response("request body too large", status_code=413)
    client: httpx.AsyncClient = request.app.state.board_client
    url = upstream_url(up, inner, request.scope.get("query_string") or b"")
    upstream_req = client.build_request(
        request.method, url, headers=_board_headers(request, drop_auth=drop_auth),
        content=body,
    )
    try:
        resp = await client.send(upstream_req, stream=True)
    except httpx.PoolTimeout:
        return Response("board busy", status_code=503)
    except httpx.TransportError:
        # The same answer the board gives for everything it declines.
        return not_found()
    out = streamed(resp, _relay(resp))
    return out


#: How long a resolved token is reused. A page load is hundreds of asset
#: requests and each would otherwise be a gateway round trip; a revoked link
#: must stop working while somebody is still watching it. Both are satisfied
#: several seconds apart.
SLICE_RESOLVE_TTL = 5.0

#: Returned by :func:`_slice_visitor` when the link's ``?user=`` contradicts the
#: name the token was minted with.
_SLICE_MISMATCH = object()


async def _slice_proxy(request: Request, path: str, raw: bytes,
                       vault_up: str) -> Response:
    """Serve a public slice of the vault: the same child, a narrower door.

    Everything this function decides is decided from the token. It carries no
    session, mints none, and reads no cookie except the one that names an open
    slice's visitor.
    """
    app = request.app
    token = slices.token_of(path)
    if token is None:
        return not_found()
    info = await _resolve_slice(app, token)
    if info is None:
        # 404, deliberately not 403: a slice that was revoked, expired or never
        # existed should look like a URL that never existed either.
        return not_found()
    if path == slices.shell_bare(token):
        # Same reasoning as ``_vault_bare`` — every reference in the shell is
        # relative and resolves against the document's directory.
        return RedirectResponse(slices.shell(token), status_code=308)
    inner = slices.upstream_raw_path(raw)
    if inner is None:
        # The mount is in the decoded path but not in the bytes.
        return not_found()
    visitor = _slice_visitor(request, info, token)
    if visitor is _SLICE_MISMATCH:
        # Loud rather than silently mis-attributed: this link names somebody
        # the token does not.
        return Response("this link names a different visitor", status_code=400)
    client: httpx.AsyncClient = app.state.client
    url = upstream_url(vault_up, inner, request.scope.get("query_string") or b"")
    body = await request.body()
    upstream_req = client.build_request(
        request.method, url,
        headers=_slicereq_headers(request, info, visitor), content=body,
    )
    try:
        resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        return _vault_unavailable(request, "not listening yet")
    out = streamed(resp)
    _remember_slice_visitor(request, out, info, token, visitor, samesite(app))
    return out


def _slice_visitor(request: Request | WebSocket, info: dict, token: str):
    """Who this request is attributed to, or :data:`_SLICE_MISMATCH`.

    A bound token carries the name; the ``?user=`` in the URL is then a display
    of it, and a contradiction is refused. An open token learns the name from
    that same parameter on first arrival and from its own cookie afterwards, so
    one link can be handed to a group and each of them types their own.
    """
    asked = request.query_params.get(slices.USER_PARAM)
    bound = info.get("user")
    if bound:
        return _SLICE_MISMATCH if asked and asked != bound else bound
    return asked or request.cookies.get(slices.cookie_name(token))


def _remember_slice_visitor(request: Request, out: Response, info: dict,
                            token: str, visitor, samesite: str) -> None:
    """Keep an open slice's visitor name across the rest of the visit.

    Only for an open token, only when the name arrived in this request's query,
    and only on this slice's own path — the edge's sign-in and sign-out clear
    Trilium's cookies at ``path=/`` (see :func:`_clear_vault_cookies`), and a
    name scoped here by both path and cookie name is not theirs to take.
    """
    if info.get("user") or not isinstance(visitor, str) or not visitor:
        return
    if request.query_params.get(slices.USER_PARAM) != visitor:
        return
    out.set_cookie(slices.cookie_name(token), visitor, path=slices.shell(token),
                   httponly=True, secure=True, samesite=samesite)


def _slicereq_headers(request: Request, info: dict, visitor) -> dict[str, str]:
    """The forwarded headers: the slice's three, and no identity at all.

    Overwritten rather than defaulted, exactly as ``X-Awm-As`` is in
    :func:`~awm.tlsfront.proxy.req_headers`. Trilium trusts these as much as it trusts loopback,
    which is total and correct — the child binds loopback and this edge is its
    only route in — so a browser must not be able to state any of them.
    """
    hdrs = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    hdrs["X-Forwarded-Proto"] = "https"
    for name in (slices.HEADER_ROOT, slices.HEADER_USER, slices.HEADER_WRITE,
                 *CALLER_HEADERS):
        hdrs.pop(name.lower(), None)
    hdrs[slices.HEADER_ROOT] = info["note_id"]
    hdrs[slices.HEADER_WRITE] = "1" if info.get("write") else "0"
    if isinstance(visitor, str) and visitor:
        hdrs[slices.HEADER_USER] = visitor
    host = request.headers.get("host")
    if host:
        hdrs["X-Forwarded-Host"] = host
    if request.client:
        hdrs["X-Forwarded-For"] = request.client.host
    return hdrs


async def _resolve_slice(app, token: str) -> dict | None:
    """What ``token`` opens, or ``None`` if it opens nothing.

    Asked of the trilium service over the gateway on loopback as this
    service, by its token and with no ``X-Awm-As``: the gateway runs it on the
    service lane with no identity, which is what that service's
    ``_operator_only`` admits, and the identity is absent because this call did
    not cross an edge. The token is also what holds the call to this plugin's
    ``[[needs.tool]]`` entry for ``trilium.slice_resolve``.

    The answer is ``{"found": bool, "note_id", "user", "write"}``, which the
    gateway delivers as JSON or as a JSON string; anything that is not a
    ``found`` row with a note is nothing. A transport failure is not
    cached — an unreachable gateway is a 404 for as long as it is unreachable,
    and no longer.
    """
    cache = app.state.slice_cache
    now = time.monotonic()
    hit = cache.get(token)
    if hit and hit[0] > now:
        return hit[1]
    client: httpx.AsyncClient = app.state.client
    try:
        resp = await client.post(
            app.state.http_up.rstrip("/") + "/invoke",
            json={"name": "trilium",
                  "args": {"verb": "slice_resolve", "args": {"token": token}}},
            headers=_service_headers(),
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        log.debug("slice resolve failed: %s", exc)
        return None
    info = None
    if resp.status_code == 200:
        result = _invoke_result((resp.json() or {}).get("result"))
        if result.get("found") and result.get("note_id"):
            info = result
    cache[token] = (now + SLICE_RESOLVE_TTL, info)
    return info


def _service_headers() -> dict[str, str]:
    """This service's token, when the supervisor gave it one."""
    token = gatewayclient.service_token()
    return {SERVICE_TOKEN_HEADER: token} if token else {}


def _invoke_result(result) -> dict:
    """The gateway's ``result`` as a mapping, however it chose to hand it over.

    A service handler returns a dict and the gateway may deliver it either as
    JSON or as a JSON *string* — the MCP shape, where a tool's output is text.
    Anything that is neither is no slice.
    """
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return {}
    return result if isinstance(result, dict) else {}


async def _ws_proxy(ws: WebSocket) -> None:
    """Bridge a browser WebSocket to the same path on the loopback gateway, or
    on the vault, slice or tether upstream that owns the path."""
    app = ws.app
    path = ws.url.path
    target = raw = raw_target(ws.scope, path)
    if re_segments(raw, path):
        await ws.close(code=1008)
        return
    # The board speaks SSE, never a socket, and its mount is not the gateway's.
    if board.in_mount(path):
        await ws.close(code=1008)
        return
    up = app.state.ws_up
    vault_ws = getattr(app.state, "vault_ws_up", None)
    is_vault = bool(vault_ws) and vault.owns(path)
    # The same second mount on the same child as the HTTP leg. Trilium builds
    # its socket URI from ``location.pathname``, so a shell served under a
    # slice opens its socket there and nowhere else.
    is_slice = bool(vault_ws) and slices.owns(path)
    # The session socket, and a room's. Only cookie, authorization and origin
    # are forwarded below, plus the client address on this leg alone, so the
    # relay counts each guesser against its own budget.
    tether_ws = getattr(app.state, "tether_ws_up", None)
    is_tether = tether.owns(path)
    # Claimed whether or not a relay is wired — see the HTTP leg for why the
    # name is owned unconditionally.
    if tether.refused(path) or (is_tether and not tether_ws):
        await ws.close(code=1008)
        return
    if is_vault or is_slice or is_tether:
        # The client derives its socket URL from the page's own pathname, so a
        # shell served at /trilium/ opens its socket there. Same rewrite as the
        # HTTP leg, or the vault's live updates never arrive and nothing says so.
        mount = vault if is_vault else slices if is_slice else tether
        inner = mount.upstream_raw_path(raw)
        if inner is None:
            await ws.close(code=1008)
            return
        up, raw = (tether_ws if is_tether else vault_ws), inner

    public = _is_public(app)
    # `/invoke` is a POST; on the public door it has no socket form.
    if public and policy.classify(path) in (policy.Verdict.DENY, policy.Verdict.INVOKE):
        await ws.close(code=1008)
        return
    slice_info = None
    slice_token = None
    sub = None
    if is_slice:
        # No session, for the reason the HTTP leg gives: the token is the
        # credential. The socket outlives the resolve that admitted it, so a
        # revoked slice keeps an open socket until the client reconnects; what
        # reaches it through that socket is bounded by the mask either way.
        slice_token = slices.token_of(path)
        slice_info = await _resolve_slice(app, slice_token) if slice_token else None
        if slice_info is None:
            await ws.close(code=1008)
            return
    elif is_tether:
        # No session, and nothing to resolve. The path's grammar was the check
        # — the slot and the ticket in it are the relay's to recognise, and it
        # refuses a pair it did not issue before this socket becomes one.
        pass
    else:
        ok, sub = await authenticate_ws(ws, path, target)
        if not ok or (public and not policy.allows(path, sub)):
            await ws.close(code=1008)  # policy violation
            return
        # The operator session has the gateway's WebSocket, never the vault's.
        if is_vault and is_machine_sub(sub):
            await ws.close(code=1008)
            return

    fwd = ws_headers(ws)
    if slice_info is not None:
        visitor = _slice_visitor(ws, slice_info, slice_token)
        if visitor is _SLICE_MISMATCH:
            await ws.close(code=1008)
            return
        fwd.pop("authorization", None)
        fwd[slices.HEADER_ROOT] = slice_info["note_id"]
        fwd[slices.HEADER_WRITE] = "1" if slice_info.get("write") else "0"
        if isinstance(visitor, str) and visitor:
            fwd[slices.HEADER_USER] = visitor
    elif is_tether:
        # No identity to forward, and the default would be the edge asserting
        # one it never checked. The relay wants none: what admits this socket
        # is a slot and a ticket it issued itself. The address goes as the
        # plain leg sends it, because the relay keys its miss budget on it.
        fwd.update(client_headers(ws))
    else:
        fwd["X-Awm-As"] = as_header(sub)
    await bridge(ws, ws_url(up, raw, query_of(ws)), fwd)


def build_app(upstream: str, ca_path: str, *,
              profile: str | None = None,
              vault_upstream: str | None = None,
              relay_upstream: str | None = None,
              board_upstream: str | None = None) -> Starlette:
    """Assemble the gateway's front on :func:`awm.tlsfront.proxy.build_app`.

    ``profile="public"`` builds the internet-facing door: no CA download, no
    landing page (``/`` sends a signed-in browser to the vault),
    ``SameSite=Strict`` cookies, and every proxied path checked against
    :mod:`awm.httpsfront.policy` — a path off the list is 404 whether or not
    the caller is signed in.

    ``vault_upstream`` adds the shared knowledge base as a *second* upstream on
    this same listener, mounted at :data:`vault.SHELL`. It is what makes one
    origin, one session and one login cover both awm and the vault. Off by
    default.

    ``relay_upstream`` adds the channel relay, mounted at
    :data:`tether.PREFIX` and :data:`tether.ROOMS` — and unlike the vault, its paths are reachable
    with **no session**, because the person redeeming an invite is a friend
    being helped with their own machine and has no awm account. The relay does
    its own gating; see :mod:`awm.httpsfront.tether` for what that rests on.
    Off by default, and left off wherever no relay is running: this is the one
    mount whose whole surface is public, so it should be present only where
    somebody meant it to be.

    ``board_upstream`` adds the federation board, mounted at :data:`board.PREFIX`
    and, like the tether, reachable with no edge session: the board
    authenticates its own parties and the edge passes their ``Authorization``
    through. Only the method-and-path shapes in :mod:`awm.httpsfront.board`
    are forwarded. Off by default; the mount answers 404 until it is wired.
    """
    public = profile == PUBLIC_PROFILE
    routes: list[BaseRoute] = []
    if public:
        routes.append(Route("/", _public_home, methods=["GET"]))
    else:
        # Authenticated landing page, and the tag/filter endpoints behind it.
        routes.append(Route("/", _root, methods=["GET"]))
        routes.append(Route("/__landing/tags", gated(_landing_add_tag), methods=["POST"]))
        routes.append(Route("/__landing/tags", gated(_landing_remove_tag), methods=["DELETE"]))
        routes.append(Route("/__landing/filter", gated(_landing_select_filter), methods=["POST"]))
        routes.append(Route("/__landing/filter", gated(_landing_deselect_filter), methods=["DELETE"]))
        routes.append(Route("/__landing/name", gated(_landing_set_name), methods=["POST"]))
    if vault_upstream:
        # Before the catch-all, and after /__auth/*: these three are the edge's
        # own answers on paths the vault would otherwise be asked for.
        routes.append(Route(vault.SHELL_BARE, _vault_bare, methods=["GET"]))
        routes.append(Route(vault.MANIFEST, gated(_vault_manifest), methods=["GET"]))
        routes.append(Route(vault.LOGOUT, _vault_logout, methods=["GET", "POST"]))

    @asynccontextmanager
    async def _board_lifespan(app_: Starlette):
        app_.state.board_client = _board_client() if board_upstream else None
        try:
            yield
        finally:
            if app_.state.board_client is not None:
                await app_.state.board_client.aclose()

    app = build_front(
        upstream, ca_path, routes=routes,
        http_handler=_http_proxy, ws_handler=_ws_proxy,
        ca_download=not public,
        same_site="strict" if public else "lax",
        identity_cookies=VAULT_COOKIES,
        lifespan=_board_lifespan,
    )
    app.state.profile = profile
    #: token -> (expires_at, resolved | None). Per app, so a test builds its own.
    app.state.slice_cache = {}
    if vault_upstream:
        v = vault_upstream.rstrip("/")
        app.state.vault_http_up = v
        app.state.vault_ws_up = "ws" + v[len("http"):]
    else:
        app.state.vault_http_up = None
        app.state.vault_ws_up = None
    if relay_upstream:
        t = relay_upstream.rstrip("/")
        app.state.tether_http_up = t
        app.state.tether_ws_up = "ws" + t[len("http"):]
    else:
        app.state.tether_http_up = None
        app.state.tether_ws_up = None
    app.state.board_http_up = board_upstream.rstrip("/") if board_upstream else None
    return app


def serve(*, port: int, cert: str, key: str, ca: str, upstream: str,
          profile: str | None = None,
          tls: bool = True,
          vault_upstream: str | None = None,
          relay_upstream: str | None = None,
          board_upstream: str | None = None) -> None:
    """Build the front and serve it on ``port`` forever (blocks); see
    :func:`awm.tlsfront.proxy.serve_app` for ``tls=False``."""
    app = build_app(upstream, ca, profile=profile,
                    vault_upstream=vault_upstream,
                    relay_upstream=relay_upstream,
                    board_upstream=board_upstream)
    serve_app(app, port=port, cert=cert, key=key, tls=tls,
              label=f"{upstream} (profile {profile or 'default'})")
