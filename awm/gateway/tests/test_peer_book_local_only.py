"""Only this node changes its own peer book.

Every ``peer`` write verb refuses a caller that carries a peer identity (the
legacy bare ``peer`` or ``peer:<node>``), domestic or foreign, on each door a
caller can reach it through: the handler, ``catalog.dispatch`` and the HTTP
route. The local CLI (no stamp) and local agents (their own stamps) keep working.
"""

from __future__ import annotations

import json

import pytest

import awm.config as cfg
from awm.gateway import catalog, gateway_ops
from awm.gateway.gateway_ops import (
    GATEWAY_OPERATIONS,
    PeerGrantRequest,
    PeerJoinRequest,
    PeerSetRequest,
    bound_caller,
)

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

PEER_STAMPS = ["peer", "peer:capella", "peer:mira", "peer:", "peer:nobody", " Peer:capella"]
LOCAL_STAMPS = [None, "", "user:operator", "agent:awm/dev", "placed-1"]


@pytest.fixture(autouse=True)
def book(tmp_path, monkeypatch):
    """A book with a domestic ``mira`` and a foreign ``envoy``; ``capella`` is absent."""
    monkeypatch.setattr(cfg, "AWM_DIR", tmp_path)
    for var in ("AWM_NODE_ROLE", "AWM_SWARM"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "state" / "peers.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "mira": {"name": "mira", "edge_url": "https://mira:1", "ssh_alias": "mira",
                 "relation": "domestic"},
        "envoy": {"name": "envoy", "edge_url": "https://envoy:1", "ssh_alias": "envoy",
                  "relation": "foreign", "swarm": "blue", "grants": []},
    }))
    return path


def _names():
    return set(json.loads((cfg.AWM_DIR / "state" / "peers.json").read_text()))


# verb -> (handler, request builder). add and join share a request shape.
WRITES = {
    "join": (gateway_ops._op_peer_join,
             lambda: PeerJoinRequest(name="orion", edge_url="orion:1")),
    "add": (gateway_ops._op_peer_add,
            lambda: PeerJoinRequest(name="vega", edge_url="vega:1")),
    "set": (gateway_ops._op_peer_set,
            lambda: PeerSetRequest(name="mira", role="station")),
    "grant": (gateway_ops._op_peer_grant,
              lambda: PeerGrantRequest(name="envoy", category="kb")),
    "revoke": (gateway_ops._op_peer_revoke,
               lambda: PeerGrantRequest(name="envoy", category="kb")),
    "forget": (gateway_ops._op_peer_forget, lambda: "mira"),
}


def _call(verb):
    handler, build = WRITES[verb]
    return handler(build())


def test_the_write_table_covers_every_effect_write_peer_op():
    declared = {op.cli_command for op in GATEWAY_OPERATIONS
                if op.cli_group == "peer" and op.effect == "write"}
    assert declared == set(WRITES)


@pytest.mark.parametrize("stamp", PEER_STAMPS)
@pytest.mark.parametrize("verb", sorted(WRITES))
def test_a_peer_identity_is_refused_and_the_book_is_untouched(verb, stamp, book):
    before = book.read_text()
    with bound_caller(stamp), pytest.raises(PermissionError, match="peer book"):
        _call(verb)
    assert book.read_text() == before


@pytest.mark.parametrize("stamp", LOCAL_STAMPS)
@pytest.mark.parametrize("verb", sorted(WRITES))
def test_no_stamp_and_a_local_stamp_may_write(verb, stamp):
    with bound_caller(stamp):
        out = _call(verb)
    assert isinstance(out, dict) and out


def test_no_binding_at_all_may_write():
    assert _call("add")["peer"]["name"] == "vega"
    assert "vega" in _names()


def test_the_binding_is_released_after_the_block():
    with bound_caller("peer:capella"):
        pass
    assert _call("add")["peer"]["name"] == "vega"


def test_the_peer_reads_stay_open_to_a_peer_identity():
    ops = {op.cli_command: op for op in GATEWAY_OPERATIONS if op.cli_group == "peer"}
    assert {ops[v].effect for v in ("list", "resolve", "providers")} == {"read"}
    with bound_caller("peer:mira"):
        assert gateway_ops._op_peer_list()["peers"]
        assert gateway_ops._op_peer_resolve("mira")["peer"]["name"] == "mira"


# ---------------------------------------------------------------------------
# The doors: catalog.dispatch (the /invoke path) and the generated HTTP routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stamp", ["peer", "peer:mira"])
async def test_dispatch_refuses_a_domestic_or_legacy_peer(stamp, book):
    before = book.read_text()
    with pytest.raises(PermissionError):
        await catalog.dispatch("peer_add", {"name": "vega", "edge_url": "vega:1"}, as_=stamp)
    with pytest.raises(PermissionError):
        await catalog.dispatch("peer_forget", {"name": "mira"}, as_=stamp)
    assert book.read_text() == before


async def test_dispatch_refuses_a_foreign_peer_as_an_unknown_tool(book):
    before = book.read_text()
    with pytest.raises(ValueError, match="Unknown tool"):
        await catalog.dispatch("peer_add", {"name": "vega", "edge_url": "vega:1"},
                               as_="peer:envoy")
    assert book.read_text() == before


@pytest.mark.parametrize("stamp", [None, "agent:awm/dev"])
async def test_dispatch_runs_a_local_write(stamp):
    out = json.loads(await catalog.dispatch(
        "peer_add", {"name": "vega", "edge_url": "vega:1"}, as_=stamp))
    assert out["peer"]["name"] == "vega"
    assert "vega" in _names()


@pytest.fixture()
def client(awm_workspace):
    from fastapi.testclient import TestClient

    from awm.gateway.server import app
    book_path = awm_workspace["awm_dir"] / "state" / "peers.json"
    book_path.parent.mkdir(parents=True, exist_ok=True)
    book_path.write_text(json.dumps({
        "mira": {"name": "mira", "edge_url": "https://mira:1", "ssh_alias": "mira",
                 "relation": "domestic"}}))
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize("stamp", ["peer", "peer:mira", "peer:capella"])
def test_http_routes_refuse_a_peer_identity_with_403(client, stamp):
    headers = {"X-Awm-As": stamp}
    body = {"name": "vega", "edge_url": "vega:1"}
    assert client.post("/peers", json=body, headers=headers).status_code == 403
    assert client.post("/peers/add", json=body, headers=headers).status_code == 403
    assert client.post("/peers/set", json={"name": "mira", "role": "station"},
                       headers=headers).status_code == 403
    grant = {"name": "mira", "category": "kb"}
    assert client.post("/peers/grant", json=grant, headers=headers).status_code == 403
    assert client.post("/peers/revoke", json=grant, headers=headers).status_code == 403
    assert client.delete("/peers/mira", headers=headers).status_code == 403
    assert client.get("/peers/mira", headers=headers).status_code == 200


def test_http_routes_serve_the_local_cli(client):
    r = client.post("/peers/add", json={"name": "vega", "edge_url": "vega:1"})
    assert r.status_code == 200 and r.json()["peer"]["name"] == "vega"
    assert client.delete("/peers/vega").status_code == 200


def test_invoke_answers_403_for_a_peer_identity(client):
    r = client.post("/invoke", headers={"X-Awm-As": "peer:mira"},
                    json={"name": "peer_forget", "args": {"name": "mira"}})
    assert r.status_code == 403
    assert "peer book" in json.dumps(r.json())
    ok = client.post("/invoke", json={"name": "peer_add",
                                      "args": {"name": "vega", "edge_url": "vega:1"}})
    assert ok.status_code == 200


def test_a_read_verb_without_a_category_is_closed_to_every_grant():
    """The goal reads declare no category, so no grant opens them to a foreign peer."""
    assert catalog._allowed("read", None, frozenset({"journals", "kb"})) is False
    assert catalog._allowed("read", "journals", frozenset({"journals"})) is True
