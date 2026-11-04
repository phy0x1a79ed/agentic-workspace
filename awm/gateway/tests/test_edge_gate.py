"""The edge gate in ``catalog.dispatch``, ``/tools`` and the ``/svc`` doors.

A caller stamped ``peer:<node>`` is served only when ``<node>`` holds an edge to
this node in the fleet edge list (``awm.config.edges``). Such a caller gets the
whole surface; any other node, and the legacy bare ``peer`` that names no node,
is refused exactly like an unknown tool. An absent identity is not affected. The edge list
comes from the autouse ``fleet_edges`` fixture: ``mira`` has an edge both ways,
``hub`` is a node with no edge to this one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awm.gateway import catalog, peer_catalog
from awm.gateway.hub.registry import ServiceRecord

EDGELESS = "peer:hub"
EDGE = "peer:mira"


class _StubRegistry:
    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


class _FakeChannel:
    def __init__(self):
        self.calls = []

    async def call(self, fn, args, as_=None, timeout=None):
        self.calls.append((fn, args, as_))
        return {"ran": fn}


def _fn(name, effect=None):
    spec = {"name": name, "description": f"{name}.",
            "params": [{"name": "q", "type": "string"}]}
    if effect:
        spec["effect"] = effect
    return spec


def _rec(name, *fns):
    return ServiceRecord(name=name, prefix=f"/svc/{name}", kind="service",
                         service_id=f"sid-{name}", api={"functions": list(fns)})


@pytest.fixture()
def world(awm_workspace, monkeypatch):
    """kb (search: read, add: write)."""
    awm_dir = awm_workspace["awm_dir"]
    stub = _StubRegistry([_rec("kb", _fn("search", "read"), _fn("add", "write"))])
    monkeypatch.setattr(catalog, "get_registry", lambda: stub)
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    monkeypatch.setattr(peer_catalog, "_snapshot", {})
    return ch


@pytest.mark.parametrize("as_", [EDGELESS, "peer:", "peer:  ", "peer:stranger", "peer", " Peer "])
@pytest.mark.parametrize("name,args", [
    ("kb_search", {"q": "x"}), ("kb", {"verb": "search"}), ("kb", {"verb": "describe"}),
    ("nope", {})])
async def test_a_node_without_an_edge_is_refused_like_an_unknown_tool(world, as_, name, args):
    with pytest.raises(ValueError, match="Unknown tool"):
        await catalog.dispatch(name, args, as_=as_)
    assert world.calls == []


@pytest.mark.parametrize("as_", [None, EDGE, "placed-1"])
async def test_an_edge_peer_and_local_callers_get_the_whole_surface(world, as_):
    await catalog.dispatch("kb_add", {}, as_=as_)
    await catalog.dispatch("kb", {"verb": "add"}, as_=as_)
    assert [c[0] for c in world.calls] == ["add", "add"]


async def test_a_removed_edge_bites_on_the_next_call(world, monkeypatch, tmp_path):
    await catalog.dispatch("kb_search", {}, as_=EDGE)
    bare = tmp_path / "bare.yml"
    bare.write_text("nodes: {}\nedges: {}\n")
    monkeypatch.setenv("AWM_EDGES_FILE", str(bare))
    with pytest.raises(ValueError, match="Unknown tool"):
        await catalog.dispatch("kb_search", {}, as_=EDGE)


# ---------------------------------------------------------------------------
# HTTP level
# ---------------------------------------------------------------------------


@pytest.fixture()
def http(world):
    from fastapi.testclient import TestClient

    from awm.gateway.server import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _post(http, name, args, as_):
    return http.post("/invoke", json={"name": name, "args": args},
                     headers={"X-Awm-As": as_} if as_ else {})


def test_invoke_is_a_404_without_an_edge_and_a_200_with_one(http, world):
    resp = _post(http, "kb_search", {"q": "x"}, EDGELESS)
    assert resp.status_code == 404 and "Unknown" in resp.json()["detail"]
    assert _post(http, "kb_search", [1], EDGELESS).status_code == 404
    assert _post(http, "kb_add", {}, "peer").status_code == 404
    for as_ in (None, EDGE):
        assert _post(http, "kb_add", {}, as_).status_code == 200


def test_tools_is_a_404_without_an_edge(http, world):
    for view in ({}, {"view": "domains"}, {"view": "domains", "peers": 1}):
        assert http.get("/tools", params=view,
                        headers={"X-Awm-As": EDGELESS}).status_code == 404
    names = {t["name"] for t in http.get(
        "/tools", params={"view": "domains"}, headers={"X-Awm-As": EDGE}).json()["tools"]}
    assert "kb" in names


# ---------------------------------------------------------------------------
# The /svc backstop
# ---------------------------------------------------------------------------


class _Req:
    url = SimpleNamespace(path="/svc/kb/fn/search")

    async def body(self):
        return b""


async def test_the_svc_doors_refuse_a_node_without_an_edge(world, monkeypatch):
    from awm.gateway.hub import proxy

    monkeypatch.setattr(proxy.rpc, "get_control", lambda sid: None)

    for call in (proxy.proxy_service_http(_Req(), "sid-kb", as_=EDGELESS),
                 proxy.open_session_via_http(_Req(), "sid-kb", as_=EDGELESS),
                 proxy.proxy_service_http(_Req(), "sid-kb", as_="peer:"),
                 proxy.proxy_service_http(_Req(), "sid-kb", as_="peer:stranger"),
                 proxy.proxy_service_http(_Req(), "sid-kb", as_="peer")):
        resp = await call
        assert resp.status_code == 404
    # edge and local callers get past the backstop (no control channel here)
    for as_ in (None, EDGE):
        resp = await proxy.proxy_service_http(_Req(), "sid-none", as_=as_)
        assert resp.status_code == 503


async def test_the_svc_sockets_close_a_node_without_an_edge_before_accepting(world):
    from awm.gateway.hub import proxy

    class WS:
        def __init__(self):
            self.closed, self.accepted = None, False

        async def close(self, code=1000, reason=""):
            self.closed = code

        async def accept(self):
            self.accepted = True

    for call in (lambda ws: proxy.proxy_service_emit_ws(ws, "sid-kb", "t", as_=EDGELESS),
                 lambda ws: proxy.proxy_session_ws(ws, "sid-kb", "abc", as_=EDGELESS)):
        ws = WS()
        await call(ws)
        assert ws.closed == 1008 and not ws.accepted


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


async def test_the_sweep_lists_each_edge_peer_as_waiting_for_the_rust_gateway(world):
    import asyncio

    snap = await asyncio.wait_for(peer_catalog.sweep(), 5)
    assert "mira" in snap and "hub" not in snap
    assert snap["mira"]["reachable"] is False
    assert "Rust gateway" in snap["mira"]["error"]

