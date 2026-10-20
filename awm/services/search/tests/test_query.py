"""Querying: ranking, filters, live sources, grants, and the fan-out to peers."""

from __future__ import annotations

import asyncio

import pytest

from awm.gatewayclient import GatewayCallError
from awm.search import federation, store
from awm.search.query import parse

from .conftest import doc, every, flat, synced

pytestmark = [pytest.mark.search]

HIT_KEYS = {"source", "id", "title", "ts", "score", "match", "passages", "origin"}


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
    return [(h["source"], h["id"]) for h in every(result)]


async def test_one_ranked_list_spans_every_source(loaded):
    result = await loaded.query(parse({"query": "kitchen sink"}))
    assert {s for s, _ in ids(result)} == {"journals", "precedence", "kb"}
    for h in every(result):
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
    result = await loaded.query(parse({"query": "kitchen", "limit": 1}))
    assert all(len(hits) == 1 for s in ("exact", "related") for hits in result[s].values())
    assert result["exact"] and result["related"]
    assert parse({"query": "x", "limit": 5000}).limit == 100


async def test_each_source_is_reported_apart_with_its_own_limit(loaded):
    result = await loaded.query(parse({"query": "kitchen", "limit": 1, "sources": "journals,precedence,kb"}))
    sections = {s: {src: [h["id"] for h in hits] for src, hits in result[s].items()} for s in ("exact", "related")}
    for section in sections.values():
        assert all(len(ids) == 1 for ids in section.values())
        assert set(section) <= {"journals", "precedence", "kb"}
    assert set(sections["exact"]) == {"journals", "precedence", "kb"}
    assert all(h["source"] == src for s in ("exact", "related", "graph") for src, hits in result[s].items()
               for h in hits)
    wide = await loaded.query(parse({"query": "kitchen", "limit": 10, "sources": "journals,kb"}))
    assert len(wide["exact"]["journals"]) == 2 and len(wide["exact"]["kb"]) == 1


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


def last_query(gateway):
    return [args for _, fn, args in gateway.calls if fn == "query"][-1]


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
    assert last_query(gateway)["query"] == "kitchen"


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
    await loaded.query(parse({"query": "kitchen", "project": "awm", "after": "2026-01-01", "passages": 3}))
    assert last_query(gateway) == {"query": "kitchen", "limit": 10, "passages": 3, "project": "awm",
                                   "after": "2026-01-01"}


async def test_a_source_that_turns_live_drops_its_indexed_documents(loaded, gateway):
    gateway.plug("papers", {"z1": doc("Old abstract", "kitchen sink abstract")})
    await synced(loaded, gateway)
    assert ("papers", "z1") in ids(await loaded.query(parse({"query": "kitchen sink"})))
    gateway.stop("papers")
    gateway.plug_live("papers", lambda args: {"exact": [{"source": "papers", "id": "doi:1", "title": "New"}]})
    await loaded.refresh()
    assert ids(await loaded.query(parse({"query": "kitchen sink", "sources": ["papers"]}))) == [("papers", "doi:1")]
    conn = store.connect()
    try:
        assert store.document_ids(conn, "papers") == set()
    finally:
        conn.close()


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
    assert every(result) == [] and result["sources"] == {"precedence": {"status": "absent"}}
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


async def test_no_peers_named_searches_this_node_only(loaded, peers):
    peers.add("altair", peer_hits("r1"))
    for value in (None, False, "", "local", ["local"]):
        args = {"query": "kitchen"} if value is None else {"query": "kitchen", "peers": value}
        result = await loaded.query(parse(args))
        assert result["peers"] == {} and every(result)
    assert peers.calls == []


async def test_named_peers_are_asked_apart_and_their_hits_join_the_list(loaded, peers):
    peers.add("altair", peer_hits("r1", "r2"))
    peers.add("shaula", peer_hits("r3"), relation="foreign", swarm="other")
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals"],
                                       "peers": "local,altair,shaula"}))
    assert result["peers"]["altair"] == {"status": "ok", "hits": 2, "sources": ["journals"]}
    remote = {h["id"]: h for h in every(result) if h["origin"]["node"] != "local"}
    assert set(remote) == {"r1", "r2", "r3"}
    assert remote["r1"]["origin"] == {"node": "altair", "swarm": "tony"}
    assert remote["r3"]["origin"] == {"node": "shaula", "swarm": "other"}
    assert "secret" not in remote["r1"]
    assert any(h["origin"]["node"] == "local" for h in every(result))


async def test_a_named_peer_alone_runs_the_query_there_only(loaded, peers):
    peers.add("shaula", peer_hits("r3"), relation="foreign", swarm="other")
    result = await loaded.query(parse({"query": "kitchen", "peers": "shaula"}))
    assert [h["id"] for h in every(result)] == ["r3"]
    assert result["sources"] == {}


async def test_fleet_asks_every_domestic_node_and_never_a_foreign_one(loaded, peers):
    peers.add("capella", peer_hits("r1"))
    peers.add("mira", peer_hits("r2"), role="station")
    peers.add("shaula", peer_hits("r3"), relation="foreign", swarm="other")
    result = await loaded.query(parse({"query": "kitchen", "peers": "fleet"}))
    assert result["peers"] == {"capella": {"status": "ok", "hits": 1, "sources": ["journals"]},
                               "mira": {"status": "station"}}
    assert [c[0] for c in peers.calls] == ["capella"]
    assert any(h["origin"]["node"] == "local" for h in every(result))


async def test_a_peer_is_asked_the_same_question_without_a_further_fan_out(loaded, peers):
    peers.add("altair", peer_hits())
    await loaded.query(parse({"query": "kitchen", "sources": ["journals"], "project": "awm", "limit": 4,
                              "peers": "altair"}))
    (name, tool, args), = peers.calls
    assert (name, tool) == ("altair", "search_query")
    assert args == {"query": "kitchen", "limit": 4, "passages": 1, "peers": False, "project": "awm",
                    "sources": ["journals"]}


async def test_a_release_without_search_is_no_search_at_home_and_unsupported_abroad(loaded, peers):
    def missing(args):
        return GatewayCallError(404, '{"detail":"Unknown tool: search_query"}')

    peers.add("capella", missing)
    peers.add("shaula", missing, relation="foreign", swarm="other")
    result = await loaded.query(parse({"query": "kitchen", "peers": "local,capella,shaula"}))
    assert result["peers"]["capella"]["status"] == "no-search"
    assert result["peers"]["shaula"]["status"] == "unsupported"
    assert any(h["origin"]["node"] == "local" for h in every(result))


async def test_a_failing_or_slow_peer_is_a_status(loaded, peers, monkeypatch):
    monkeypatch.setattr(federation, "PEER_TIMEOUT_S", 0.05)

    async def slow(args):
        await asyncio.sleep(5)

    peers.add("down", lambda args: ConnectionError("refused"))
    peers.add("junk", lambda args: {"detail": "nope"})
    peers.add("big", lambda args: "x" * (federation.MAX_REPLY_BYTES + 1))
    peers.add("slow", slow)
    result = await loaded.query(parse({"query": "kitchen", "peers": "fleet"}))
    assert "refused" in result["peers"]["down"]["error"]
    assert "no hits" in result["peers"]["junk"]["error"]
    assert "exceeds" in result["peers"]["big"]["error"]
    assert "timeout" in result["peers"]["slow"]["error"]
    assert every(result)


async def test_a_call_from_a_peer_searches_this_node_only(loaded, peers):
    peers.add("altair", peer_hits("r1"))
    for as_ in ("peer:altair", "peer"):
        result = await loaded.query(parse({"query": "kitchen", "peers": "fleet"}, as_))
        assert result["peers"] == {}
    assert peers.calls == []


async def test_named_peers_are_asked_and_unknown_ones_reported(loaded, peers):
    peers.add("altair", peer_hits("r1"))
    peers.add("deneb", peer_hits("r2"))
    result = await loaded.query(parse({"query": "kitchen", "peers": ["altair", "ghost"]}))
    assert set(result["peers"]) == {"altair", "ghost"}
    assert result["peers"]["ghost"]["error"] == "not in the peer book"
    assert [c[0] for c in peers.calls] == ["altair"]


async def test_a_peer_answering_with_one_list_per_source_joins_each_source_apart(loaded, peers):
    peers.add("altair", lambda args: {
        "exact": {"journals": [{"source": "journals", "id": "far1", "title": "Kitchen elsewhere"}],
                  "kb": [{"source": "kb", "id": "doi:9", "title": "A far paper"}]},
        "related": {}, "graph": {}})
    result = await loaded.query(parse({"query": "kitchen", "peers": ["altair", "local"], "limit": 5}))
    assert {h["id"] for h in result["exact"]["journals"]} >= {"far1", "j1"}
    assert [h["origin"]["node"] for h in result["exact"]["kb"] if h["id"] == "doi:9"] == ["altair"]


def papers_home(args):
    if "query" not in args:
        return {"sources": [{"name": "papers", "service": "papers", "mode": "live"},
                            {"name": "journals", "service": "journal", "mode": "index"}]}
    assert args["sources"] == ["papers"] and args["peers"] is False
    return {"exact": [{"source": "papers", "id": "doi:1", "title": "A paper",
                       "passages": [{"text": "a kitchen tap", "start": 0, "end": 13}]}],
            "related": [], "graph": []}


async def test_a_domain_homed_elsewhere_is_asked_of_its_home_alone(loaded, peers, monkeypatch):
    monkeypatch.setenv("AWM_DOMAIN_HOME_papers", "altair")
    monkeypatch.setenv("AWM_DOMAIN_HOME_kb", "altair")
    peers.add("altair", papers_home)
    await loaded.refresh_homes(force=True)
    assert loaded.homed == {"papers": "altair"}
    result = await loaded.query(parse({"query": "kitchen"}))
    assert ("papers", "doi:1") in ids(result)
    assert result["sources"]["papers"] == {"status": "ok", "hits": 1, "via": "altair"}
    assert result["peers"] == {}
    named = await loaded.query(parse({"query": "kitchen", "sources": ["papers"]}))
    assert ids(named) == [("papers", "doi:1")] and flat(named["exact"])[0]["origin"]["node"] == "altair"


async def test_a_relayed_call_asks_no_home(loaded, peers, monkeypatch):
    monkeypatch.setenv("AWM_DOMAIN_HOME_papers", "altair")
    peers.add("altair", papers_home)
    await loaded.refresh_homes(force=True)
    calls = len(peers.calls)
    result = await loaded.query(parse({"query": "kitchen"}, "peer:capella"))
    assert "papers" not in result["sources"] and len(peers.calls) == calls


async def test_an_unreachable_home_keeps_its_sources_and_reports_the_error(loaded, peers, monkeypatch):
    monkeypatch.setenv("AWM_DOMAIN_HOME_papers", "altair")
    peers.add("altair", papers_home)
    await loaded.refresh_homes(force=True)
    peers.answers["altair"] = lambda args: ConnectionError("no route to host")
    await loaded.refresh_homes(force=True)
    assert loaded.homed == {"papers": "altair"}
    result = await loaded.query(parse({"query": "kitchen"}))
    assert result["sources"]["papers"]["status"] == "error" and result["sources"]["papers"]["via"] == "altair"
    assert ids(result)


async def test_get_with_a_node_reads_that_peers_archive(peers):
    peers.add("shaula", lambda args: {"document": {"title": "far", "text": "body", "origin": {"node": "liar"}}},
              relation="foreign", swarm="other")
    got = await federation.fetch(peers, "shaula", "journals", "r3")
    assert got["document"]["origin"] == {"node": "shaula", "swarm": "other"}
    assert peers.calls == [("shaula", "search_get", {"source": "journals", "id": "r3"})]


# -- PageRank ----------------------------------------------------------------


async def test_kb_ranks_each_section_for_the_context_and_never_changes_its_hits(loaded, gateway):
    plain = await loaded.query(parse({"query": "kitchen"}))
    asked = []

    def rank(args):
        asked.append(args)
        return {"profile": "scope:awm/dev + project:awm", "scores": [float(i) for i in range(len(args["hits"]))]}

    gateway.handlers[("kb", "rank")] = rank
    ranked = await loaded.query(parse({"query": "kitchen", "context": "awm/dev"}))
    assert asked[-1]["context"] == "awm/dev"
    assert set(asked[-1]["hits"][0]) == {"source", "id", "project", "scope"}
    assert ranked["rank"] == {"profile": "scope:awm/dev + project:awm"}
    for section in ("exact", "related", "graph"):
        assert {s: sorted(h["id"] for h in hits) for s, hits in ranked[section].items()} == \
            {s: sorted(h["id"] for h in hits) for s, hits in plain[section].items()}


async def test_a_failed_rank_leaves_the_answer_in_its_section_order(loaded, gateway):
    gateway.handlers[("kb", "rank")] = lambda args: RuntimeError("no graph")
    result = await loaded.query(parse({"query": "kitchen"}))
    assert "no graph" in result["rank"]["error"] and ids(result)


async def test_no_kb_asks_for_no_rank(archive, gateway):
    gateway.plug("journals", {"j1": doc("Kitchen", "kitchen sink", kind="journal")})
    await synced(archive, gateway)
    result = await archive.query(parse({"query": "kitchen"}))
    assert "rank" not in result and not any(fn == "rank" for _, fn, _ in gateway.calls)
