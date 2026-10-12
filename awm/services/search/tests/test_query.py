"""Querying: ranking, filters, live sources, grants, and the fan-out to peers."""

from __future__ import annotations

import asyncio

import pytest

from awm.gatewayclient import GatewayCallError
from awm.search import federation
from awm.search.query import parse

from .conftest import doc, synced

pytestmark = [pytest.mark.search]

HIT_KEYS = {"source", "id", "title", "snippet", "ts", "score", "origin"}


@pytest.fixture()
async def loaded(archive, gateway):
    gateway.plug("journals", {
        "j1": doc("Kitchen refit", "we decided to refit the kitchen sink and tap", "2026-03-01T10:00:00Z",
                  project="awm", scope="dev", kind="journal"),
        "j2": doc("Garden shed", "the garden shed roof was replaced", "2026-05-01T10:00:00Z",
                  project="home", scope="yard", kind="journal"),
        "j3": doc("Plumbing note", "a note about the kitchen plumbing", "2026-04-01T10:00:00Z",
                  project="awm", scope="dev", kind="message")}, topic="posts")
    gateway.plug("precedence", {"p1": doc("Sink choice", "the user prefers a deep kitchen sink", "2026-02-01T00:00:00Z",
                                          kind="decision")})
    gateway.plug("kb", {"k1": doc("Fluid dynamics of taps", "papers on kitchen tap flow", "2020-01-01T00:00:00Z",
                                  kind="paper", url="https://doi.org/10.1/x")})
    await synced(archive, gateway)
    return archive


def ids(result):
    return [(h["source"], h["id"]) for h in result["hits"]]


async def test_one_ranked_list_spans_every_source(loaded):
    result = await loaded.query(parse({"query": "kitchen sink"}))
    assert {s for s, _ in ids(result)} == {"journals", "precedence", "kb"}
    for h in result["hits"]:
        assert HIT_KEYS <= set(h)
        assert h["origin"] == {"node": "local", "swarm": "tony"}
    assert result["sources"]["journals"]["status"] == "ok"
    assert result["peers"] == {}


async def test_a_paraphrase_finds_the_journal(loaded):
    result = await loaded.query(parse({"query": "kitchen sink refit", "sources": ["journals"]}))
    assert ids(result)[0] == ("journals", "j1")


async def test_sources_narrow_the_search(loaded):
    result = await loaded.query(parse({"query": "kitchen", "sources": "kb,precedence"}))
    assert {s for s, _ in ids(result)} <= {"kb", "precedence"}
    assert set(result["sources"]) == {"kb", "precedence"}


async def test_an_unknown_source_is_reported_absent(loaded):
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals", "nothing"]}))
    assert result["sources"]["nothing"] == {"status": "absent"}
    assert ids(result)


@pytest.mark.parametrize("filters, expected", [
    ({"project": "awm"}, {"j1", "j3"}),
    ({"scope": "yard"}, {"j2"}),
    ({"kind": "message"}, {"j3"}),
    ({"after": "2026-04-01"}, {"j2", "j3"}),
    ({"before": "2026-04-01"}, {"j1"}),
    ({"after": "2026-03-15T00:00:00+00:00", "before": "2026-04-15"}, {"j3"}),
])
async def test_filters_apply_before_ranking(loaded, filters, expected):
    result = await loaded.query(parse({"query": "kitchen garden roof plumbing", "sources": ["journals"], **filters}))
    assert {i for _, i in ids(result)} == expected


async def test_a_bad_date_is_refused():
    with pytest.raises(ValueError, match="after"):
        parse({"query": "x", "after": "last tuesday"})


async def test_limit_is_clamped(loaded):
    assert len((await loaded.query(parse({"query": "kitchen", "limit": 1})))["hits"]) == 1
    assert parse({"query": "x", "limit": 5000}).limit == 100


async def test_a_frozen_source_still_answers_and_says_so(loaded, gateway):
    gateway.stop("kb")
    await loaded.refresh()
    result = await loaded.query(parse({"query": "tap flow", "sources": ["kb"]}))
    assert ids(result) == [("kb", "k1")]
    assert result["sources"]["kb"]["status"] == "frozen"


async def test_get_returns_the_stored_document(loaded):
    got = (await loaded.get("kb", "k1"))["document"]
    assert got["title"] == "Fluid dynamics of taps" and got["url"] == "https://doi.org/10.1/x"
    with pytest.raises(ValueError, match="no document"):
        await loaded.get("kb", "nope")
    with pytest.raises(ValueError, match="no source"):
        await loaded.get("missing", "k1")


# -- live sources ----------------------------------------------------------


def transcripts(args):
    return {"hits": [{"id": "s1:7", "title": "awm", "snippet": f"you said {args['query']}",
                      "ts": "2026-06-01T00:00:00+00:00", "project": "awm"}]}


async def test_a_live_source_runs_only_when_named_if_it_is_opt_in(loaded, gateway):
    gateway.plug_live("transcripts", transcripts, optin=True)
    await loaded.refresh()
    default = await loaded.query(parse({"query": "kitchen"}))
    assert "transcripts" not in default["sources"]
    named = await loaded.query(parse({"query": "kitchen", "sources": ["transcripts"]}))
    assert ids(named) == [("transcripts", "s1:7")]
    assert named["sources"]["transcripts"] == {"status": "ok", "hits": 1}
    assert gateway.calls[-1][2]["query"] == "kitchen"


async def test_a_live_source_that_is_not_opt_in_joins_the_default_search(loaded, gateway):
    gateway.plug_live("web", transcripts)
    await loaded.refresh()
    result = await loaded.query(parse({"query": "kitchen"}))
    assert ("web", "s1:7") in ids(result)


async def test_a_failing_live_source_never_hides_the_archive(loaded, gateway):
    gateway.plug_live("web", lambda args: RuntimeError("boom"))
    await loaded.refresh()
    result = await loaded.query(parse({"query": "kitchen"}))
    assert result["sources"]["web"]["status"] == "error" and "boom" in result["sources"]["web"]["error"]
    assert ids(result)


async def test_a_malformed_live_reply_is_an_error_status(loaded, gateway):
    gateway.plug_live("web", lambda args: {"results": []})
    await loaded.refresh()
    assert (await loaded.query(parse({"query": "kitchen"})))["sources"]["web"]["status"] == "error"


async def test_filters_reach_a_live_source(loaded, gateway):
    gateway.plug_live("web", transcripts)
    await loaded.refresh()
    await loaded.query(parse({"query": "kitchen", "project": "awm", "after": "2026-01-01"}))
    assert gateway.calls[-1][2] == {"query": "kitchen", "limit": 10, "project": "awm", "after": "2026-01-01"}


# -- grants ----------------------------------------------------------------


async def test_a_grant_limits_every_read_to_its_sources(loaded):
    result = await loaded.query(parse({"query": "kitchen", "_grants": ["journals"]}, "peer:shaula"))
    assert {s for s, _ in ids(result)} == {"journals"}
    assert set(result["sources"]) == {"journals"}
    listed = await loaded.sources_view(frozenset({"journals"}))
    assert [s["name"] for s in listed["sources"]] == ["journals"]
    status = await loaded.status_view(frozenset({"journals"}))
    assert status["sources"] == {"journals": 3} and "registry" not in status


async def test_a_named_source_outside_the_grants_looks_absent(loaded):
    result = await loaded.query(parse({"query": "kitchen", "sources": ["precedence"], "_grants": ["journals"]},
                                      "peer:shaula"))
    assert result["hits"] == [] and result["sources"] == {"precedence": {"status": "absent"}}
    with pytest.raises(ValueError, match="no source"):
        await loaded.get("precedence", "p1", frozenset({"journals"}))


async def test_no_grants_list_means_every_source(loaded):
    assert parse({"query": "x"}).grants is None
    assert parse({"query": "x", "_grants": []}).grants == frozenset()


# -- peers -----------------------------------------------------------------


def peer_hits(*docs, sources=("journals",)):
    return lambda args: {"hits": [{"source": "journals", "id": d, "title": f"remote {d}",
                                   "snippet": "from afar", "ts": "2026-06-01T00:00:00Z", "score": 0.9,
                                   "origin": {"node": "liar", "swarm": "elsewhere"}, "secret": "x"}
                                  for d in docs],
                         "sources": {s: {"status": "ok"} for s in sources}, "peers": {}}


async def test_peers_are_asked_apart_and_their_hits_join_the_list(loaded, peers):
    peers.add("altair", peer_hits("r1", "r2"))
    peers.add("shaula", peer_hits("r3"), relation="foreign", swarm="other")
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals"]}))
    assert result["peers"]["altair"] == {"status": "ok", "hits": 2, "sources": ["journals"]}
    remote = {h["id"]: h for h in result["hits"] if h["origin"]["node"] != "local"}
    assert set(remote) == {"r1", "r2", "r3"}
    assert remote["r1"]["origin"] == {"node": "altair", "swarm": "tony"}
    assert remote["r3"]["origin"] == {"node": "shaula", "swarm": "other"}
    assert "secret" not in remote["r1"]
    assert any(h["origin"]["node"] == "local" for h in result["hits"])


async def test_a_peer_is_asked_the_same_question_without_a_further_fan_out(loaded, peers):
    peers.add("altair", peer_hits())
    await loaded.query(parse({"query": "kitchen", "sources": ["journals"], "project": "awm", "limit": 4}))
    (name, tool, args), = peers.calls
    assert (name, tool) == ("altair", "search_query")
    assert args == {"query": "kitchen", "limit": 4, "peers": False, "project": "awm", "sources": ["journals"]}


async def test_an_older_peer_is_unsupported_and_local_hits_remain(loaded, peers):
    peers.add("shaula", lambda args: GatewayCallError(404, '{"detail":"Unknown tool: search_query"}'))
    peers.add("altair", peer_hits("r1"))
    result = await loaded.query(parse({"query": "kitchen"}))
    assert result["peers"]["shaula"]["status"] == "unsupported"
    assert result["peers"]["altair"]["status"] == "ok"
    assert any(h["origin"]["node"] == "local" for h in result["hits"])


async def test_a_failing_or_slow_peer_is_a_status(loaded, peers, monkeypatch):
    monkeypatch.setattr(federation, "PEER_TIMEOUT_S", 0.05)

    async def slow(args):
        await asyncio.sleep(5)

    peers.add("down", lambda args: ConnectionError("refused"))
    peers.add("junk", lambda args: {"detail": "nope"})
    peers.add("big", lambda args: "x" * (federation.MAX_REPLY_BYTES + 1))
    peers.add("slow", slow)
    result = await loaded.query(parse({"query": "kitchen"}))
    assert "refused" in result["peers"]["down"]["error"]
    assert "no hits" in result["peers"]["junk"]["error"]
    assert "exceeds" in result["peers"]["big"]["error"]
    assert "timeout" in result["peers"]["slow"]["error"]
    assert result["hits"]


async def test_peers_false_asks_nobody(loaded, peers):
    peers.add("altair", peer_hits("r1"))
    result = await loaded.query(parse({"query": "kitchen", "peers": False}))
    assert peers.calls == [] and result["peers"] == {}


async def test_a_call_from_a_peer_searches_this_node_only(loaded, peers):
    peers.add("altair", peer_hits("r1"))
    for as_ in ("peer:altair", "peer"):
        result = await loaded.query(parse({"query": "kitchen"}, as_))
        assert result["peers"] == {}
    assert peers.calls == []


async def test_named_peers_are_asked_and_unknown_ones_reported(loaded, peers):
    peers.add("altair", peer_hits("r1"))
    peers.add("deneb", peer_hits("r2"))
    result = await loaded.query(parse({"query": "kitchen", "peers": ["altair", "ghost"]}))
    assert set(result["peers"]) == {"altair", "ghost"}
    assert result["peers"]["ghost"]["error"] == "not in the peer book"
    assert [c[0] for c in peers.calls] == ["altair"]


async def test_this_node_is_never_asked(monkeypatch):
    monkeypatch.setattr(federation.config, "list_records",
                        lambda: [{"name": "Local.lan"}, {"name": "altair"}])
    assert [r["name"] for r in federation.Peers().records()] == ["altair"]
