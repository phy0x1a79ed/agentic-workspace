"""Node identity at the edge: who a bearer proves, and what each kind of peer reaches.

What these pin: a node token verifies against the book's key for its issuer and
only for this node's name, and yields ``peer:<node>``; the legacy bearer still
yields bare ``peer`` until ``AWM_PEER_LEGACY_BEARER=0`` retires it; a domestic
node is a peer in every way the legacy bearer was (the gateway, never the vault,
Penpot or a person's files); a foreign node, or one the book does not list,
reaches ``/invoke`` and ``/tools`` (the gateway's effect gate limits what it may
call there) and gets a 404 for everything else the mesh edge serves, WebSockets
included.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager

import httpx
import pytest
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from awm.config import peertoken, tokens
from awm.httpsfront import policy, proxy
from awm.httpsfront.auth import (
    COOKIE_NAME,
    AuthGate,
    is_foreign_peer,
    is_machine_sub,
    is_peer_sub,
)

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

ME = "altair"
SECRET = "edge-secret"
LEGACY = "legacy-peer-credential"
SESSION = "signed.session"


@pytest.fixture(autouse=True)
def node(monkeypatch, peer_book):
    monkeypatch.setenv("AWM_NODE_NAME", ME)
    peer_book({"mira": {"relation": "domestic"}, "shaula": {"relation": "foreign"}})


@pytest.fixture()
def keys():
    private = peertoken.generate_private_key()
    return private, peertoken.public_key_of(private)


def _gate(*, peers, flag=None) -> AuthGate:
    gate = AuthGate()
    gate._material = {
        "secret": SECRET,
        "peer_credentials": [LEGACY],
        "peers": peers,
        "session_ttl_seconds": 3600.0,
        "max_session_seconds": 0,
        **({} if flag is None else {"legacy_bearer": flag}),
    }
    gate._fetched_at = time.monotonic()
    return gate


def _auth(gate, bearer=None, cookie=None):
    return asyncio.run(gate.authenticate(cookie=cookie, bearer=bearer))


def _peer(name, public, relation="domestic"):
    return {"name": name, "public_key": public, "relation": relation}


# -- the gate -----------------------------------------------------------------

def test_a_node_token_authenticates_as_peer_colon_node(keys):
    private, public = keys
    token = peertoken.sign(private, iss="mira", aud=ME)
    assert _auth(_gate(peers=[_peer("mira", public)]), token) == (True, None, "peer:mira")


def test_a_foreign_nodes_token_authenticates_too(keys):
    private, public = keys
    token = peertoken.sign(private, iss="shaula", aud=ME)
    gate = _gate(peers=[_peer("shaula", public, "foreign")])
    assert _auth(gate, token)[2] == "peer:shaula"


@pytest.mark.parametrize("why", ["wrong audience", "expired", "wrong key",
                                 "unlisted issuer", "no key on file"])
def test_a_token_that_should_not_verify_does_not(keys, why):
    private, public = keys
    other_private = peertoken.generate_private_key()
    peers = [_peer("mira", public)]
    if why == "wrong audience":
        token = peertoken.sign(private, iss="mira", aud="somebody-else")
    elif why == "expired":
        token = peertoken.sign(private, iss="mira", aud=ME, now=time.time() - 3600)
    elif why == "wrong key":
        token = peertoken.sign(other_private, iss="mira", aud=ME)
    elif why == "unlisted issuer":
        token = peertoken.sign(private, iss="rigel", aud=ME)
    else:
        token = peertoken.sign(private, iss="mira", aud=ME)
        peers = [_peer("mira", None)]
    assert _auth(_gate(peers=peers), token) == (False, None, None)


def test_a_node_token_is_not_accepted_as_a_legacy_bearer(keys):
    private, _ = keys
    token = peertoken.sign(private, iss="mira", aud=ME)
    gate = _gate(peers=[])
    gate._material["peer_credentials"] = [token]
    assert _auth(gate, token)[0] is False


def test_the_legacy_bearer_is_bare_peer_while_it_is_on():
    assert _auth(_gate(peers=[]), LEGACY) == (True, None, "peer")


def test_the_legacy_bearer_is_rejected_once_retired_by_environment(monkeypatch):
    monkeypatch.setenv("AWM_PEER_LEGACY_BEARER", "0")
    assert _auth(_gate(peers=[]), LEGACY) == (False, None, None)


def test_the_legacy_bearer_is_rejected_once_retired_by_material():
    assert _auth(_gate(peers=[], flag=False), LEGACY) == (False, None, None)


def test_retiring_the_bearer_leaves_node_tokens_and_sessions_alone(monkeypatch, keys):
    monkeypatch.setenv("AWM_PEER_LEGACY_BEARER", "0")
    private, public = keys
    gate = _gate(peers=[_peer("mira", public)])
    assert _auth(gate, peertoken.sign(private, iss="mira", aud=ME))[2] == "peer:mira"
    cookie = tokens.mint(SECRET, sub="tony", ttl=3600)
    assert _auth(gate, cookie=cookie)[2] == "tony"


def test_a_bad_bearer_does_not_hide_a_good_cookie():
    cookie = tokens.mint(SECRET, sub="tony", ttl=3600)
    assert _auth(_gate(peers=[]), "garbage", cookie)[2] == "tony"


# -- the identity predicates --------------------------------------------------

def test_every_peer_identity_is_a_machine():
    for sub in ("peer", "peer:mira", "peer:shaula", "peer:nobody"):
        assert is_peer_sub(sub) and is_machine_sub(sub), sub
    assert is_machine_sub("operator") and not is_peer_sub("operator")
    for sub in ("tony", "peerless", "", None):
        assert not is_machine_sub(sub), sub


RELATIONS = {"mira": "domestic", "shaula": "foreign"}.get


def test_only_unlisted_and_foreign_nodes_are_foreign():
    assert not is_foreign_peer("peer", RELATIONS)
    assert not is_foreign_peer("peer:mira", RELATIONS)
    assert is_foreign_peer("peer:shaula", RELATIONS)
    assert is_foreign_peer("peer:nobody", RELATIONS)
    assert not is_foreign_peer("tony", RELATIONS)
    assert not is_foreign_peer(None, RELATIONS)


def test_a_node_with_no_relation_source_is_foreign():
    assert is_foreign_peer("peer:mira")
    assert not is_foreign_peer("peer")


def test_the_relation_comes_from_the_material_snapshot_not_the_book(peer_book, keys):
    """The book on disk says mira is foreign now; the snapshot that held her key
    said domestic, and that is what this request is judged by."""
    peer_book({"mira": {"relation": "foreign"}})
    _, public = keys
    gate = _gate(peers=[_peer("mira", public, "domestic")])
    assert not is_foreign_peer("peer:mira", gate.peer_relation)
    gate = _gate(peers=[_peer("mira", public, "foreign")])
    assert is_foreign_peer("peer:mira", gate.peer_relation)
    assert gate.peer_relation("rigel") is None


def test_the_foreign_door_is_tools_and_invoke_exactly():
    assert policy.FOREIGN_PATHS == frozenset({"/tools", "/invoke"})
    assert policy.foreign_allows("/tools")
    assert policy.foreign_allows("/invoke")
    for path in ("/", "/invoke/", "/peers", "/tools/invoke", "/invoke/x", "/tools/x", "/hub/services",
                 "/svc/notes/fn/list", "/trilium/", "/penpot/", "/files/x",
                 "/__auth/whoami"):
        assert not policy.foreign_allows(path), path


def test_an_fqdn_hostname_and_a_bare_book_name_agree(monkeypatch, keys):
    """This node's name may be an FQDN and the issuer's book name bare, or the
    reverse; the token and the edge compare the first label, lowercased."""
    monkeypatch.setenv("AWM_NODE_NAME", "Altair.Lab.Example.com")
    private, public = keys
    gate = _gate(peers=[_peer("Mira.lab.example.com", public)])
    token = peertoken.sign(private, iss="MIRA", aud="altair")
    assert _auth(gate, token) == (True, None, "peer:Mira.lab.example.com")
    token = peertoken.sign(private, iss="mira.lab.example.com",
                           aud="altair.lab.example.com")
    assert _auth(gate, token)[0] is True
    assert _auth(gate, peertoken.sign(private, iss="mira", aud="altair2"))[0] is False


def test_a_machine_is_not_a_person_on_the_public_policy():
    for sub in ("peer", "peer:mira", "peer:shaula", "operator"):
        assert not policy.allows("/trilium/", sub), sub
        assert not policy.allows("/penpot/", sub), sub
    assert policy.allows("/trilium/", "tony")


# -- the edge, end to end ------------------------------------------------------

GATEWAY = "http://127.0.0.1:7819"
VAULT = "http://127.0.0.1:12511"
PENPOT = "http://127.0.0.1:9001"

BEARERS = {"b-legacy": "peer", "b-mira": "peer:mira", "b-shaula": "peer:shaula",
           "b-nobody": "peer:nobody",
           "awmpt1.mira-token": "peer:mira", "awmpt1.shaula-token": "peer:shaula"}

#: Headers only a local process may set, spelled in the cases a client might try.
CALLER_HEADERS = {"X-Awm-Caller-Pid": "1", "x-awm-session-pid": "2",
                  "X-AWM-PEER-REDIRECT": "1"}


class _Gate:
    async def authenticate(self, *, cookie=None, bearer=None):
        if bearer in BEARERS:
            return True, None, BEARERS[bearer]
        if cookie == SESSION:
            return True, None, "tony"
        return False, None, None

    def peer_relation(self, node):
        return RELATIONS(node)

    async def session_ttl_seconds(self):
        return 3600.0

    async def verify_password(self, password):
        return None


async def _body():
    yield b"ok"


async def _extra(request):
    return PlainTextResponse("privileged")


@contextmanager
def _edge(bearer=None):
    app = proxy.build_app(GATEWAY + "/", "/dev/null", profile=None,
                          vault_upstream=VAULT, penpot_upstream=PENPOT,
                          extra_routes=[("/signin", ["GET"], _extra)])
    app.state.gate = _Gate()
    seen: list = []
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("x-awm-as")))
        sent.append({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, content=_body())

    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    with TestClient(app, base_url="https://mesh.example", follow_redirects=False,
                    headers=headers) as c:
        c.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        c.sent = sent
        yield c, seen


GATEWAY_PATHS = ["/svc/notes/fn/list", "/hub/services", "/files/projects/x",
                 "/svc/drawio/emit/t", "/ui/drawio/", "/"]
VAULT_PENPOT = ["/trilium/", "/trilium/api/tree", "/penpot/", "/penpot/api/rpc/x"]


@pytest.mark.parametrize("bearer", ["b-legacy", "b-mira"])
def test_a_domestic_node_reaches_the_gateway_as_the_legacy_bearer_did(bearer):
    with _edge(bearer) as (c, seen):
        for path in ("/invoke", "/tools", "/svc/notes/fn/list", "/hub/services",
                     "/files/projects/x"):
            assert c.get(path).status_code == 200, path
    assert {s[1] for s in seen} == {BEARERS[bearer]}
    assert all(url.startswith(GATEWAY) for url, _ in seen)


@pytest.mark.parametrize("bearer", ["b-legacy", "b-mira", "b-shaula", "b-nobody"])
@pytest.mark.parametrize("path", VAULT_PENPOT)
def test_no_peer_reaches_the_vault_or_penpot(bearer, path):
    with _edge(bearer) as (c, seen):
        assert c.get(path).status_code == 404
    assert seen == []


@pytest.mark.parametrize("bearer", ["b-shaula", "b-nobody"])
def test_a_foreign_node_reaches_tools_only(bearer):
    with _edge(bearer) as (c, seen):
        assert c.get("/tools").status_code == 200
        assert c.get("/tools", params={"view": "domains"}).status_code == 200
    assert [s[1] for s in seen] == [BEARERS[bearer]] * 2


@pytest.mark.parametrize("bearer", ["b-shaula", "b-nobody"])
def test_a_foreign_node_reaches_invoke_and_the_gateway_decides(bearer):
    """The edge forwards `/invoke` stamped with the caller's identity; the
    gateway's effect gate, not the edge, refuses what its grants do not cover."""
    with _edge(bearer) as (c, seen):
        assert c.post("/invoke", json={"name": "kb_search", "args": {}}).status_code == 200
    assert [s[1] for s in seen] == [BEARERS[bearer]]
    assert seen[0][0].startswith(GATEWAY + "/invoke")


@pytest.mark.parametrize("bearer", ["b-shaula", "b-nobody"])
@pytest.mark.parametrize("path", ["/svc/notes/fn/list", "/svc/auth/fn/edge_material",
                                  "/peers", "/tools/peers", "/invoke/", "/invoke/x",
                                  "/hub/services", "/hub/register", "/files/x"])
@pytest.mark.parametrize("method", ["get", "post", "put", "delete"])
def test_a_foreign_node_still_reaches_nothing_beyond_tools_and_invoke(
        bearer, path, method):
    with _edge(bearer) as (c, seen):
        assert getattr(c, method)(path).status_code == 404
    assert seen == []


@pytest.mark.parametrize("bearer", ["b-shaula", "b-nobody"])
@pytest.mark.parametrize("query", ["peers=1", "peers", "view=domains&peers=0"])
@pytest.mark.parametrize("path", ["/tools", "/invoke"])
def test_a_foreign_node_cannot_widen_either_door_to_the_fleet(bearer, path, query):
    with _edge(bearer) as (c, seen):
        assert c.get(f"{path}?{query}").status_code == 404
    assert seen == []


@pytest.mark.parametrize("bearer", ["b-shaula", "b-nobody"])
@pytest.mark.parametrize("query", ["peers=1", "peers=0", "view=domains&peers=1",
                                   "%70eers=1", "peers"])
def test_a_foreign_node_cannot_ask_for_the_fleet_view(bearer, query):
    with _edge(bearer) as (c, seen):
        assert c.get(f"/tools?{query}").status_code == 404
    assert seen == []


@pytest.mark.parametrize("bearer", ["b-legacy", "b-mira"])
def test_a_domestic_node_keeps_the_fleet_view_and_invoke(bearer):
    with _edge(bearer) as (c, seen):
        assert c.get("/tools?peers=1&view=domains").status_code == 200
        assert c.post("/invoke", json={"name": "x", "args": {}}).status_code == 200
    assert len(seen) == 2


@pytest.mark.parametrize("bearer", ["b-shaula", "b-nobody"])
@pytest.mark.parametrize("path", GATEWAY_PATHS + ["/__auth/whoami", "/signin",
                                                  "/invoke/", "/tools/x"])
def test_a_foreign_node_gets_404_for_everything_else(bearer, path):
    with _edge(bearer) as (c, seen):
        assert c.get(path).status_code == 404
    assert seen == []


def test_a_person_is_unaffected_by_the_foreign_door():
    with _edge() as (c, seen):
        c.cookies.set(COOKIE_NAME, SESSION, domain="mesh.example")
        assert c.get("/svc/notes/fn/list").status_code == 200
        assert c.get("/signin").status_code == 200
        assert c.get("/__auth/whoami").json() == {"user": "tony"}
    assert seen[0][1] == "user:tony"


def test_a_domestic_node_still_sees_whoami_and_extra_routes():
    with _edge("b-mira") as (c, _):
        assert c.get("/__auth/whoami").json() == {"user": "peer:mira"}
        assert c.get("/signin").status_code == 200


@pytest.mark.parametrize("bearer,path,reaches", [
    ("b-mira", "/svc/drawio/emit/t", True),
    ("b-legacy", "/svc/drawio/emit/t", True),
    ("b-shaula", "/svc/drawio/emit/t", False),
    ("b-nobody", "/svc/drawio/emit/t", False),
    ("b-mira", "/trilium/", False),
    ("b-mira", "/penpot/ws/notifications", False),
    ("b-shaula", "/trilium/", False),
])
def test_websockets_by_peer_kind(monkeypatch, bearer, path, reaches):
    dialled: list = []

    async def connect(url, *, additional_headers=None, **kw):
        dialled.append((url, dict(additional_headers or {})))
        raise RuntimeError("upstream refused")

    monkeypatch.setattr(proxy.websockets, "connect", connect)
    with _edge() as (c, _):
        try:
            with c.websocket_connect(path, headers={"Authorization": f"Bearer {bearer}"}):
                pass
        except Exception:  # noqa: BLE001 — the refusal is the path under test
            pass
    assert bool(dialled) is reaches
    if reaches:
        assert dialled[0][1]["X-Awm-As"] == BEARERS[bearer]


# -- headers that name a local session ----------------------------------------

@pytest.mark.parametrize("bearer", [None, "b-legacy", "b-mira", "awmpt1.mira-token"])
def test_caller_pid_headers_never_cross_the_edge(bearer):
    """Reflection and `cx start` act on the session a pid names. A remote caller
    that could set one would act on any local session."""
    with _edge(bearer) as (c, seen):
        c.cookies.set(COOKIE_NAME, SESSION, domain="mesh.example")
        assert c.get("/svc/notes/fn/list", headers=CALLER_HEADERS).status_code == 200
    (sent,) = c.sent
    for name in CALLER_HEADERS:
        assert name.lower() not in sent, name
    assert not [k for k in sent if k.startswith("x-awm-") and k != "x-awm-as"]


def test_caller_pid_headers_are_stripped_on_a_foreign_nodes_tools_call_too():
    with _edge("b-shaula") as (c, _):
        assert c.get("/tools", headers=CALLER_HEADERS).status_code == 200
    (sent,) = c.sent
    assert not [k for k in sent if k.startswith("x-awm-") and k != "x-awm-as"]
    assert sent["x-awm-as"] == "peer:shaula"


@pytest.mark.parametrize("bearer", ["b-shaula", "b-nobody"])
def test_a_foreign_nodes_invoke_reaches_the_gateway_with_only_the_edges_stamp(bearer):
    forged = {"X-Awm-Peer-Redirect": "1", "X-Awm-Session-Pid": "4242",
              "X-Awm-Caller-Pid": "4243", "X-Awm-As": "user:tony"}
    with _edge(bearer) as (c, seen):
        assert c.post("/invoke", json={"name": "kb_search", "args": {}},
                      headers=forged).status_code == 200
    (sent,) = c.sent
    assert sent["x-awm-as"] == BEARERS[bearer]
    assert not [k for k in sent if k.startswith("x-awm-") and k != "x-awm-as"]
    assert [s[0].startswith(GATEWAY + "/invoke") for s in seen] == [True]


def test_duplicate_caller_headers_are_all_dropped():
    with _edge("b-mira") as (c, _):
        c.get("/svc/x", headers=[("X-Awm-Session-Pid", "1"), ("x-awm-session-pid", "2"),
                                 ("X-Awm-As", "user:tony"), ("x-awm-as", "user:root")])
    (sent,) = c.sent
    assert "x-awm-session-pid" not in sent and sent["x-awm-as"] == "peer:mira"


@pytest.mark.parametrize("bearer", [None, "b-legacy", "b-mira"])
def test_caller_pid_headers_do_not_ride_a_websocket(monkeypatch, bearer):
    dialled: list = []

    async def connect(url, *, additional_headers=None, **kw):
        dialled.append(dict(additional_headers or {}))
        raise RuntimeError("upstream refused")

    monkeypatch.setattr(proxy.websockets, "connect", connect)
    headers = dict(CALLER_HEADERS, cookie=f"{COOKIE_NAME}={SESSION}")
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    with _edge() as (c, _):
        try:
            with c.websocket_connect("/svc/drawio/emit/t", headers=headers):
                pass
        except Exception:  # noqa: BLE001
            pass
    assert dialled
    lowered = {k.lower() for k in dialled[0]}
    assert not lowered & {n.lower() for n in CALLER_HEADERS}


# -- the node token is for the edge only --------------------------------------

def test_a_consumed_node_token_is_not_forwarded_over_http():
    with _edge("awmpt1.mira-token") as (c, _):
        assert c.get("/svc/notes/fn/list").status_code == 200
    assert "authorization" not in c.sent[0]
    assert c.sent[0]["x-awm-as"] == "peer:mira"


def test_a_node_token_is_not_forwarded_when_a_cookie_carried_the_request():
    """The bearer was not what authenticated this call, but it is still a live
    credential somewhere, and an upstream app has no use for it."""
    with _edge("awmpt1.not-a-real-token") as (c, _):
        c.cookies.set(COOKIE_NAME, SESSION, domain="mesh.example")
        assert c.get("/svc/notes/fn/list").status_code == 200
    assert "authorization" not in c.sent[0]
    assert c.sent[0]["x-awm-as"] == "user:tony"


@pytest.mark.parametrize("bearer", ["b-legacy"])
def test_the_legacy_bearer_is_forwarded_exactly_as_before(bearer):
    with _edge(bearer) as (c, _):
        assert c.get("/svc/notes/fn/list").status_code == 200
    assert c.sent[0]["authorization"] == f"Bearer {bearer}"


def test_a_session_users_own_authorization_is_forwarded_as_before():
    with _edge() as (c, _):
        c.cookies.set(COOKIE_NAME, SESSION, domain="mesh.example")
        c.get("/svc/notes/fn/list", headers={"Authorization": "Basic dGVzdDp0ZXN0"})
    assert c.sent[0]["authorization"] == "Basic dGVzdDp0ZXN0"


@pytest.mark.parametrize("bearer,forwarded", [
    ("awmpt1.mira-token", False), ("b-legacy", True)])
def test_a_node_token_is_not_forwarded_over_a_websocket(monkeypatch, bearer, forwarded):
    dialled: list = []

    async def connect(url, *, additional_headers=None, **kw):
        dialled.append({k.lower(): v for k, v in (additional_headers or {}).items()})
        raise RuntimeError("upstream refused")

    monkeypatch.setattr(proxy.websockets, "connect", connect)
    with _edge() as (c, _):
        try:
            with c.websocket_connect("/svc/drawio/emit/t",
                                     headers={"Authorization": f"Bearer {bearer}"}):
                pass
        except Exception:  # noqa: BLE001
            pass
    assert dialled
    assert ("authorization" in dialled[0]) is forwarded
