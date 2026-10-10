"""The board reached through the edge, with no edge session.

What these pin: only the board's seven method-and-path shapes are forwarded, with
a card id of exactly 32 lowercase hex digits, and every other path under the
mount is a 404 before the board is asked; the party's ``Authorization`` rides
through and the edge never consults its own login or node-token gate for the
mount; every ``X-Awm-*`` header and the edge cookie are stripped; the body is
capped; the board's own 404, 409 and 400 pass through; the SSE stream is relayed
chunk by chunk with ``Last-Event-ID`` intact and no read timeout; the door is
shut unless ``AWM_EDGE_BOARD`` wires it; and the public profile still refuses
``/invoke`` and everything else it refused before.
"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import contextmanager

import httpx
import pytest
from starlette.testclient import TestClient

from awm.httpsfront import board, policy, proxy

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

GATEWAY = "http://127.0.0.1:7819"
BOARD = "http://127.0.0.1:12521"

CARD = "0123456789abcdef0123456789abcdef"
BEARER = "party-bearer-token"
SESSION = "signed.session"
LEGACY = "legacy-node-wide-bearer"

SHAPES = [
    ("POST", "/board/cards"),
    ("GET", "/board/cards"),
    ("GET", f"/board/cards/{CARD}"),
    ("POST", f"/board/cards/{CARD}/claim"),
    ("POST", f"/board/cards/{CARD}/complete"),
    ("POST", f"/board/cards/{CARD}/fail"),
    ("GET", "/board/stream"),
]


class _Gate:
    """The edge's own gate. The board mount never asks it who a caller is; it
    asks only which bearers are mesh credentials, so it can keep them out."""

    consulted = 0
    legacy: list[str] | None = [LEGACY]

    async def peer_credentials(self):
        return type(self).legacy

    async def authenticate(self, *, cookie=None, bearer=None):
        type(self).consulted += 1
        if cookie == SESSION:
            return True, None, "tony"
        return False, None, None

    def peer_relation(self, node):
        return None

    async def session_ttl_seconds(self):
        return 3600.0

    async def verify_password(self, password):
        return None


@pytest.fixture(autouse=True)
def _reset_gate():
    _Gate.consulted = 0
    _Gate.legacy = [LEGACY]


def _app(*, profile="public", upstream=BOARD):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=profile,
                          board_upstream=upstream)
    app.state.gate = _Gate()
    return app


def _recorder(status=200, body=b'{"ok": true}', headers=None):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({
            "url": str(request.url),
            "method": request.method,
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "content": request.content,
        })
        # A stream, as a real transport hands back: the edge reads it with aiter_raw.
        return httpx.Response(status, stream=httpx.ByteStream(body), headers=headers or {})

    return seen, handler


@contextmanager
def _client(app, handler):
    c = TestClient(app, base_url="https://board.example", follow_redirects=False)
    with c:
        c.real_board_client = c.app.state.board_client
        c.shared_client = c.app.state.client
        c.app.state.board_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        yield c


def _send(c, method, path, **kw):
    return c.request(method, path, **kw)


# -- the allowed shapes -----------------------------------------------------------

@pytest.mark.parametrize("profile", ["public", None])
@pytest.mark.parametrize("method,path", SHAPES)
def test_each_allowed_shape_is_forwarded_untouched(profile, method, path):
    seen, handler = _recorder()
    with _client(_app(profile=profile), handler) as c:
        r = _send(c, method, path, headers={"Authorization": f"Bearer {BEARER}"})
    assert r.status_code == 200
    assert len(seen) == 1
    assert seen[0]["url"] == BOARD + path
    assert seen[0]["method"] == method
    assert seen[0]["headers"]["authorization"] == f"Bearer {BEARER}"
    assert _Gate.consulted == 0


def test_the_list_query_and_the_body_arrive_as_sent():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get("/board/cards?status=posted&recipient=tony&limit=5",
              headers={"Authorization": f"Bearer {BEARER}"})
        c.post("/board/cards", json={"kind": "request", "recipient": "tony", "title": "t"},
               headers={"Authorization": f"Bearer {BEARER}"})
    assert seen[0]["url"] == BOARD + "/board/cards?status=posted&recipient=tony&limit=5"
    assert b'"recipient"' in seen[1]["content"]
    assert seen[1]["headers"]["content-type"] == "application/json"


def test_the_board_answers_pass_through_as_they_are():
    for status, body in ((404, b'{"error": "not found"}'), (409, b'{"error": "held"}'),
                         (400, b'{"error": "bad request: x"}')):
        seen, handler = _recorder(status=status, body=body,
                                  headers={"content-type": "application/json"})
        with _client(_app(), handler) as c:
            r = c.post(f"/board/cards/{CARD}/claim",
                       headers={"Authorization": f"Bearer {BEARER}"})
        assert (r.status_code, r.content) == (status, body)


def test_a_board_that_is_down_answers_404_like_the_tether():
    def handler(request):
        raise httpx.ConnectError("refused")

    with _client(_app(), handler) as c:
        assert c.get("/board/cards").status_code == 404


def test_an_exhausted_board_pool_answers_503_at_once():
    def handler(request):
        raise httpx.PoolTimeout("no slot")

    with _client(_app(), handler) as c:
        assert c.get("/board/stream").status_code == 503


def test_the_board_has_its_own_bounded_client():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.real_board_client is not c.shared_client
        t = c.real_board_client.timeout
    assert proxy.BOARD_LIMITS.max_connections == 64
    assert (t.connect, t.write, t.pool) == (5.0, 10.0, 2.0)
    assert t.read is None


def test_board_streams_cannot_starve_the_shared_client():
    """With every board slot taken the board leg fails fast, while the gateway
    leg, on the shared client, still answers."""
    gateway_seen, gateway_handler = _recorder()

    def board_handler(request):
        raise httpx.PoolTimeout("no slot")

    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=None, board_upstream=BOARD)
    app.state.gate = _OpenGate()
    with TestClient(app, base_url="https://board.example") as c:
        c.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(gateway_handler))
        c.app.state.board_client = httpx.AsyncClient(
            transport=httpx.MockTransport(board_handler))
        assert c.get("/board/stream").status_code == 503
        assert c.get("/svc/notes/fn/list").status_code == 200
    assert gateway_seen and gateway_seen[0]["url"].startswith(GATEWAY)


class _OpenGate(_Gate):
    async def authenticate(self, *, cookie=None, bearer=None):
        return True, None, "tony"


def test_a_stream_that_dies_mid_body_ends_cleanly():
    closed: list[bool] = []

    class Upstream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b": hello\n\n"
            raise httpx.ReadError("board went away")

        async def aclose(self):
            closed.append(True)

    def handler(request):
        return httpx.Response(200, stream=Upstream(),
                              headers={"content-type": "text/event-stream"})

    with _client(_app(), handler) as c:
        r = c.get("/board/stream")
    assert r.status_code == 200
    assert r.content == b": hello\n\n"
    assert closed == [True]


# -- everything else is a 404 -----------------------------------------------------

BAD_PATHS = [
    ("GET", "/board"),
    ("GET", "/board/"),
    ("GET", "/board/cards/"),
    ("GET", "/board/stream/"),
    ("GET", "/board/health"),
    ("GET", "/board/parties"),
    ("GET", "/board/cards/short"),
    ("GET", f"/board/cards/{CARD[:-1]}"),
    ("GET", f"/board/cards/{CARD}0"),
    ("GET", f"/board/cards/{CARD.upper()}"),
    ("GET", "/board/cards/01234567-89ab-cdef-0123-456789abcdef"),
    ("GET", "/board/cards/" + "g" * 32),
    ("POST", f"/board/cards/{CARD.upper()}/claim"),
    ("POST", f"/board/cards/{CARD}/release"),
    ("POST", f"/board/cards/{CARD}/claim/extra"),
    ("POST", f"/board/cards/{CARD}/"),
    ("GET", f"/board/cards/{CARD}/claim"),
    ("GET", "/board/cards/%30" + CARD[1:]),
    ("GET", "/board/cards/%2e%2e/stream"),
    ("GET", "/board/cards%2fx"),
    ("GET", "/board//cards"),
    ("GET", "/board/cards/" + CARD + "%0a"),
    ("GET", "/boards/cards"),
]


@pytest.mark.parametrize("profile", ["public", None])
@pytest.mark.parametrize("method,path", BAD_PATHS)
def test_a_malformed_path_is_404_before_the_board_is_asked(profile, method, path):
    seen, handler = _recorder()
    with _client(_app(profile=profile), handler) as c:
        r = _send(c, method, path)
    if path.startswith("/boards"):
        # Not the mount at all: ordinary edge business, never the board.
        assert all(not s["url"].startswith(BOARD) for s in seen)
        return
    assert r.status_code == 404
    assert seen == []


@pytest.mark.parametrize("method,path", SHAPES)
def test_a_wrong_method_on_a_claimed_path_is_404(method, path):
    wrong = {"GET": ["POST", "PUT", "DELETE"], "POST": ["PUT", "DELETE", "PATCH"]}[method]
    allowed = {m for m, p in SHAPES if p == path}
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        for other in wrong:
            if other in allowed:
                continue
            assert _send(c, other, path).status_code == 404, other
    assert seen == []


def test_head_and_options_are_not_forwarded():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.head("/board/cards").status_code == 404
        assert c.options("/board/cards").status_code == 404
    assert seen == []


def test_the_policy_verdicts():
    for _, path in SHAPES:
        assert policy.classify(path) is policy.Verdict.BOARD
        assert policy.allows(path, None)
    for _, path in BAD_PATHS:
        if (path.startswith("/board/") or path == "/board") and not board.owns(path):
            assert policy.classify(path) is policy.Verdict.DENY, path
            assert not policy.allows(path, "tony"), path


def test_the_shape_helpers_agree_with_each_other():
    assert board.owns(f"/board/cards/{CARD}")
    assert not board.owns(f"/board/cards/{CARD}\n")
    assert board.allows("get", "/board/stream")
    assert not board.allows("POST", "/board/stream")
    assert board.in_mount("/board") and board.in_mount("/board/x")
    assert not board.in_mount("/boards") and not board.in_mount("/boarding/x")
    assert board.upstream_raw_path(b"/board/cards") == b"/board/cards"
    assert board.upstream_raw_path(b"/board/c%61rds") is None


# -- identity ---------------------------------------------------------------------

def test_every_awm_header_and_the_edge_cookie_are_stripped():
    seen, handler = _recorder()
    sent = {
        "Authorization": f"Bearer {BEARER}",
        "X-Awm-As": "user:tony",
        "x-awm-as-2": "peer:mira",
        "X-AWM-Caller-Pid": "1",
        "X-Awm-Session-Pid": "2",
        "x-awm-peer-redirect": "1",
        "X-Awm-Anything-Else": "1",
        "X-Forwarded-For": "6.6.6.6",
        "Cookie": f"awm_session={SESSION}",
        "Last-Event-ID": "42",
        "Accept": "text/event-stream",
    }
    with _client(_app(), handler) as c:
        r = c.get("/board/stream", headers=sent)
    assert r.status_code == 200
    got = seen[0]["headers"]
    assert not [k for k in got if k.startswith("x-awm-")], got
    assert "cookie" not in got
    assert got["authorization"] == f"Bearer {BEARER}"
    assert got["last-event-id"] == "42"
    assert got["accept"] == "text/event-stream"
    assert got["x-forwarded-for"] != "6.6.6.6"
    assert got["x-forwarded-proto"] == "https"
    assert _Gate.consulted == 0


def test_duplicate_awm_headers_are_all_dropped():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get("/board/cards", headers=[("X-Awm-As", "user:a"), ("x-awm-as", "user:b"),
                                       ("X-Awm-Session-Pid", "1"), ("x-awm-session-pid", "2")])
    assert not [k for k in seen[0]["headers"] if k.startswith("x-awm-")]


def test_a_node_token_is_not_forwarded_to_the_board():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get("/board/cards", headers={"Authorization": "Bearer awmpt1.abc.def"})
    assert "authorization" not in seen[0]["headers"]
    assert _Gate.consulted == 0


def test_the_legacy_node_wide_bearer_is_not_forwarded_to_the_board():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get("/board/cards", headers={"Authorization": f"Bearer {LEGACY}"}
                     ).status_code == 200
        c.get("/board/cards", headers={"Authorization": f"Bearer {BEARER}"})
    assert "authorization" not in seen[0]["headers"]
    assert seen[1]["headers"]["authorization"] == f"Bearer {BEARER}"


def test_a_retired_legacy_bearer_is_still_kept_from_the_board():
    """The bearer stays on file after the edge stops honouring it; the board
    must not see it either way."""
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.get("/board/cards", headers={"Authorization": f"bearer {LEGACY}"})
    assert "authorization" not in seen[0]["headers"]


def test_without_auth_material_the_edge_cannot_tell_and_refuses():
    _Gate.legacy = None
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        assert c.get("/board/cards", headers={"Authorization": f"Bearer {BEARER}"}
                     ).status_code == 404
        assert c.get("/board/cards").status_code == 200
    assert len(seen) == 1


@pytest.mark.parametrize("values", [
    [f"Bearer {BEARER}", f"Bearer {BEARER}"],
    [f"Bearer {BEARER}", "Bearer awmpt1.abc.def"],
    ["Bearer awmpt1.abc.def", f"Bearer {BEARER}"],
    [f"Bearer {BEARER}", f"Bearer {LEGACY}"],
])
def test_two_authorization_headers_are_refused(values):
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get("/board/cards", headers=[("Authorization", v) for v in values])
    assert r.status_code == 404
    assert seen == []


def test_no_edge_login_is_asked_for_and_no_session_is_needed():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.get("/board/cards")
    assert r.status_code == 200
    assert _Gate.consulted == 0


# -- the body cap -----------------------------------------------------------------

def test_a_declared_oversize_body_is_refused_unsent():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.post("/board/cards", content=b"x" * (board.MAX_BODY + 1),
                   headers={"Authorization": f"Bearer {BEARER}"})
    assert r.status_code == 413
    assert seen == []


def test_an_undeclared_oversize_body_is_refused_too():
    seen, handler = _recorder()

    def chunks():
        for _ in range(board.MAX_BODY // 65536 + 2):
            yield b"x" * 65536

    with _client(_app(), handler) as c:
        r = c.post("/board/cards", content=chunks(),
                   headers={"Authorization": f"Bearer {BEARER}"})
    assert r.status_code == 413
    assert seen == []


def test_a_body_at_the_cap_is_forwarded():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        r = c.post("/board/cards", content=b"x" * board.MAX_BODY)
    assert r.status_code == 200
    assert len(seen[0]["content"]) == board.MAX_BODY


# -- the door is off unless wired ---------------------------------------------------

@pytest.mark.parametrize("profile", ["public", None])
@pytest.mark.parametrize("method,path", SHAPES)
def test_an_unwired_door_is_404_and_reaches_nothing(profile, method, path):
    seen, handler = _recorder()
    with _client(_app(profile=profile, upstream=None), handler) as c:
        r = _send(c, method, path, headers={"Authorization": f"Bearer {BEARER}"})
    assert r.status_code == 404
    assert seen == []
    assert _Gate.consulted == 0


@pytest.mark.parametrize("raw,wired", [(None, False), ("", False), ("0", False),
                                       ("false", False), ("no", False), ("off", False),
                                       ("2", False), ("enabled", False),
                                       ("1", True), ("true", True), ("YES", True),
                                       ("On", True)])
def test_the_adapter_wires_the_door_only_for_the_edge_flag(monkeypatch, raw, wired):
    from awm.httpsfront import hub_adapter

    monkeypatch.delenv("AWM_EDGE_BOARD", raising=False)
    monkeypatch.delenv("AWM_BOARD_PORT", raising=False)
    if raw is not None:
        monkeypatch.setenv("AWM_EDGE_BOARD", raw)
    try:
        mod = importlib.reload(hub_adapter)
        assert mod.BOARD_UPSTREAM == ("http://127.0.0.1:12521" if wired else None)
        monkeypatch.setenv("AWM_BOARD_PORT", "12999")
        mod = importlib.reload(hub_adapter)
        assert mod.BOARD_UPSTREAM == ("http://127.0.0.1:12999" if wired else None)
    finally:
        monkeypatch.delenv("AWM_EDGE_BOARD", raising=False)
        monkeypatch.delenv("AWM_BOARD_PORT", raising=False)
        importlib.reload(hub_adapter)


@pytest.mark.parametrize("flag,port,expect", [
    ("0", "not-a-port", None),          # door off: the port is never read
    (None, "12x", None),
    ("1", "not-a-port", None),          # door on, bad port: stays off, no crash
    ("1", "0", None),
    ("1", "70000", None),
    ("1", "-5", None),
])
def test_a_bad_board_port_never_stops_the_edge(monkeypatch, flag, port, expect):
    from awm.httpsfront import hub_adapter

    monkeypatch.delenv("AWM_EDGE_BOARD", raising=False)
    if flag is not None:
        monkeypatch.setenv("AWM_EDGE_BOARD", flag)
    monkeypatch.setenv("AWM_BOARD_PORT", port)
    try:
        mod = importlib.reload(hub_adapter)   # must not raise
        assert mod.BOARD_UPSTREAM is expect and mod.BOARD is False
    finally:
        monkeypatch.delenv("AWM_EDGE_BOARD", raising=False)
        monkeypatch.delenv("AWM_BOARD_PORT", raising=False)
        importlib.reload(hub_adapter)


# -- what the public profile still refuses -------------------------------------------

@pytest.mark.parametrize("path", ["/invoke", "/tools", "/hub/services", "/svc/notes/fn/list",
                                  "/peers", "/files/x", "/ca.crt"])
def test_the_public_profile_still_refuses_what_it_refused(path):
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        c.cookies.set("awm_session", SESSION, domain="board.example")
        assert c.get(path).status_code == 404
        assert c.post(path, json={}).status_code == 404
    assert seen == []


def test_the_board_mount_does_not_open_a_websocket():
    seen, handler = _recorder()
    with _client(_app(), handler) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/board/stream"):
                pass
    assert seen == []


# -- the stream ----------------------------------------------------------------------

def test_the_stream_is_relayed_incrementally_with_last_event_id():
    """The upstream sends the second chunk only after the edge has handed the
    first to the client. A relay that buffered the body would wait for the
    second chunk forever, so the whole exchange runs under a timeout."""
    app = _app()
    seen: dict = {}

    async def run():
        delivered = asyncio.Event()
        sent_messages: list[dict] = []

        class Upstream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b": hello\n\n"
                await delivered.wait()
                yield b"id: 7\nevent: card.posted\ndata: {}\n\n"

        def handler(request: httpx.Request) -> httpx.Response:
            seen["last_event_id"] = request.headers.get("last-event-id")
            seen["authorization"] = request.headers.get("authorization")
            return httpx.Response(200, stream=Upstream(), headers={
                "content-type": "text/event-stream", "cache-control": "no-cache",
                "x-accel-buffering": "no"})

        app.state.board_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        requested = False

        async def receive():
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message):
            sent_messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                if b"hello" in message["body"]:
                    delivered.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "https", "path": "/board/stream",
            "raw_path": b"/board/stream", "query_string": b"", "root_path": "",
            "headers": [(b"host", b"board.example"),
                        (b"authorization", f"Bearer {BEARER}".encode()),
                        (b"last-event-id", b"6")],
            "client": ("203.0.113.9", 4444), "server": ("board.example", 443),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=10)
        return sent_messages

    messages = asyncio.run(run())
    start = messages[0]
    assert start["status"] == 200
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert headers["content-type"] == "text/event-stream"
    assert headers["x-accel-buffering"] == "no"
    bodies = [m["body"] for m in messages[1:] if m.get("body")]
    assert bodies == [b": hello\n\n", b"id: 7\nevent: card.posted\ndata: {}\n\n"]
    assert seen == {"last_event_id": "6", "authorization": f"Bearer {BEARER}"}


def test_the_upstream_is_closed_when_the_client_walks_away():
    app = _app()
    closed: list[bool] = []

    async def run():
        class Upstream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b": hello\n\n"
                await asyncio.sleep(3600)

            async def aclose(self):
                closed.append(True)

        def handler(request):
            return httpx.Response(200, stream=Upstream())

        app.state.board_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gone = asyncio.Event()

        requested = False

        async def receive():
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                gone.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "https", "path": "/board/stream",
            "raw_path": b"/board/stream", "query_string": b"", "root_path": "",
            "headers": [(b"host", b"board.example")],
            "client": ("203.0.113.9", 4444), "server": ("board.example", 443),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=10)

    asyncio.run(run())
    assert closed == [True]
