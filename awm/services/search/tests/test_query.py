"""Querying: ranking, filters, live sources, sources on other nodes, and the hop."""

from __future__ import annotations

import asyncio

import pytest

from awm.gatewayclient import GatewayCallError
from awm.search import federation, store
from awm.search.query import parse

from .conftest import HERE, doc, every, flat, synced

pytestmark = [pytest.mark.search]

HIT_KEYS = {"source", "id", "title", "ts", "score", "match", "origin"}
#: The index sources the ``loaded`` fixture registers.
LOADED = ["journals", "wiki", "kb"]


@pytest.fixture()
async def loaded(archive, gateway):
    gateway.plug("journals", {
        "j1": doc("Kitchen refit", "we decided to refit the kitchen sink and tap", "2026-03-01T10:00:00Z",
                  project="awm", scope="dev", tags=["journal"]),
        "j2": doc("Garden shed", "the garden shed roof was replaced", "2026-05-01T10:00:00Z",
                  project="home", scope="yard", tags=["journal"], reply_to=["j1"]),
        "j3": doc("Plumbing note", "a note about the kitchen plumbing", "2026-04-01T10:00:00Z",
                  project="awm", scope="dev", tags=["message"], reply_to=["j2"])}, topic="posts")
    gateway.plug("wiki", {"p1": doc("Sink choice", "the user prefers a deep kitchen sink", "2026-02-01T00:00:00Z")})
    gateway.plug("kb", {"k1": doc("Fluid dynamics of taps", "papers on kitchen tap flow", "2020-01-01T00:00:00Z",
                                  kind="paper", url="https://doi.org/10.1/x")})
    await synced(archive, gateway)
    return archive


def ids(result):
    return [(h["source"], h["id"]) for h in every(result)]


async def test_one_ranked_list_spans_every_named_source(loaded):
    result = await loaded.query(parse({"query": "kitchen sink", "sources": LOADED}))
    assert {s for s, _ in ids(result)} == {"journals", "wiki", "kb"}
    for h in every(result):
        assert HIT_KEYS <= set(h) and "passages" not in h
        assert h["origin"] == {"node": "local"}
    assert result["sources"]["journals"]["status"] == "ok"
    assert "peers" not in result and "hop" not in result


async def test_a_paraphrase_finds_the_journal(loaded):
    result = await loaded.query(parse({"query": "kitchen sink refit", "sources": ["journals"]}))
    assert ids(result)[0] == ("journals", "j1")


async def test_sources_narrow_the_search(loaded):
    result = await loaded.query(parse({"query": "kitchen", "sources": "kb,wiki"}))
    assert {s for s, _ in ids(result)} <= {"kb", "wiki"}
    assert set(result["sources"]) == {"kb", "wiki"}


async def test_an_unknown_source_is_reported_absent(loaded):
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals", "nothing"]}))
    assert result["sources"]["nothing"] == {"status": "absent"}
    assert ids(result)


@pytest.mark.parametrize("filters, expected", [
    ({"project": "awm"}, {"j1", "j3"}),
    ({"scope": "yard"}, {"j2"}),
    ({"tags": "message"}, {"j3"}),
    ({"tags": "!message"}, {"j1", "j2"}),
    ({"after": "2026-04-01"}, {"j2", "j3"}),
    ({"before": "2026-04-01"}, {"j1"}),
    ({"after": "2026-03-15T00:00:00+00:00", "before": "2026-04-15"}, {"j3"}),
])
async def test_filters_apply_before_ranking(loaded, filters, expected):
    result = await loaded.query(parse({"query": "kitchen garden roof plumbing", "sources": ["journals"], **filters}))
    assert {i for _, i in ids(result)} == expected


async def test_a_bad_date_is_refused():
    with pytest.raises(ValueError, match="after"):
        parse({"query": "x", "sources": ["journals"], "after": "last tuesday"})


async def test_limit_is_clamped(loaded):
    result = await loaded.query(parse({"query": "kitchen", "limit": 1, "sources": LOADED}))
    assert all(len(hits) == 1 for s in ("exact", "related") for hits in result[s].values())
    assert result["exact"] and result["related"]
    assert parse({"query": "x", "sources": ["journals"], "limit": 5000}).limit == 100


async def test_each_source_is_reported_apart_with_its_own_limit(loaded):
    result = await loaded.query(parse({"query": "kitchen", "limit": 1, "sources": "journals,wiki,kb"}))
    sections = {s: {src: [h["id"] for h in hits] for src, hits in result[s].items()} for s in ("exact", "related")}
    for section in sections.values():
        assert all(len(ids) == 1 for ids in section.values())
        assert set(section) <= {"journals", "wiki", "kb"}
    assert set(sections["exact"]) == {"journals", "wiki", "kb"}
    assert all(h["source"] == src for s in ("exact", "related") for src, hits in result[s].items()
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


@pytest.mark.parametrize("sources", [None, "", [], [" "]])
def test_a_query_without_sources_is_refused(sources):
    args = {"query": "kitchen"} if sources is None else {"query": "kitchen", "sources": sources}
    with pytest.raises(ValueError, match="needs `sources`"):
        parse(args)
    with pytest.raises(ValueError, match="needs `sources`"):
        parse(args, "peer:altair")


def test_a_query_needs_words_or_a_hop_from_ids():
    with pytest.raises(ValueError, match="needs a `query`, or a `hop` with `from`"):
        parse({"sources": ["journals"]})
    with pytest.raises(ValueError, match="needs a `query`"):
        parse({"sources": ["journals"], "hop": {"via": "reply_to"}})
    assert parse({"sources": ["journals"], "hop": {"via": "reply_to", "from": ["j1"]}}).text == ""


@pytest.mark.parametrize("optin", [True, False])
async def test_a_live_source_runs_only_when_named(loaded, gateway, optin):
    gateway.plug_live("transcripts", transcripts, optin=optin)
    await loaded.refresh()
    others = await loaded.query(parse({"query": "kitchen", "sources": LOADED}))
    assert "transcripts" not in others["sources"] and ("transcripts", "s1:7") not in ids(others)
    assert not any(fn == "query" for _, fn, _ in gateway.calls)
    named = await loaded.query(parse({"query": "kitchen", "sources": ["transcripts"]}))
    assert ids(named) == [("transcripts", "s1:7")]
    assert named["sources"]["transcripts"] == {"status": "ok", "hits": 1}
    assert last_query(gateway)["query"] == "kitchen"


async def test_a_failing_live_source_never_hides_the_archive(loaded, gateway):
    gateway.plug_live("web", lambda args: RuntimeError("boom"))
    await loaded.refresh()
    result = await loaded.query(parse({"query": "kitchen", "sources": [*LOADED, "web"]}))
    assert result["sources"]["web"]["status"] == "error" and "boom" in result["sources"]["web"]["error"]
    assert ids(result)


async def test_a_malformed_live_reply_is_an_error_status(loaded, gateway):
    gateway.plug_live("web", lambda args: {"results": []})
    await loaded.refresh()
    assert (await loaded.query(parse({"query": "kitchen", "sources": ["web"]})))["sources"]["web"]["status"] == "error"


async def test_filters_reach_a_live_source(loaded, gateway):
    gateway.plug_live("web", transcripts)
    await loaded.refresh()
    await loaded.query(parse({"query": "kitchen", "sources": ["web"], "project": "awm", "after": "2026-01-01"}))
    assert last_query(gateway) == {"query": "kitchen", "limit": 10, "project": "awm", "after": "2026-01-01"}


async def test_a_source_that_turns_live_drops_its_indexed_documents(loaded, gateway):
    gateway.plug("papers", {"z1": doc("Old abstract", "kitchen sink abstract")})
    await synced(loaded, gateway)
    assert ("papers", "z1") in ids(await loaded.query(parse({"query": "kitchen sink", "sources": ["papers"]})))
    gateway.stop("papers")
    gateway.plug_live("papers", lambda args: {"exact": [{"source": "papers", "id": "doi:1", "title": "New"}]})
    await loaded.refresh()
    assert ids(await loaded.query(parse({"query": "kitchen sink", "sources": ["papers"]}))) == [("papers", "doi:1")]
    conn = store.connect()
    try:
        assert store.document_ids(conn, "papers") == set()
    finally:
        conn.close()


# -- sources on other nodes ----------------------------------------------------------


def peer_hits(*docs, sources=("journals",)):
    return lambda args: {"hits": [{"source": "journals", "id": d, "title": f"remote {d}",
                                   "snippet": "from afar", "ts": "2026-06-01T00:00:00Z", "score": 0.9,
                                   "origin": {"node": "liar"}, "secret": "x"}
                                  for d in docs],
                         "sources": {s: {"status": "ok", "hits": len(docs)} for s in sources}}


async def test_bare_sources_search_this_node_only(loaded, peers):
    peers.add("altair", peer_hits("r1"))
    result = await loaded.query(parse({"query": "kitchen", "sources": LOADED}))
    assert every(result) and all(h["origin"]["node"] == HERE for h in every(result))
    assert peers.calls == []


async def test_a_source_named_with_this_node_is_this_nodes(loaded, peers):
    peers.add("altair", peer_hits("r1"))
    result = await loaded.query(parse({"query": "kitchen", "sources": [f"journals@{HERE}"]}))
    assert set(result["sources"]) == {"journals"} and peers.calls == []


async def test_a_source_at_a_node_is_asked_of_that_node_and_answers_under_its_name(loaded, peers):
    peers.add("altair", peer_hits("r1", "r2"))
    peers.add("shaula", peer_hits("r3"))
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals", "journals@altair",
                                                                        "journals@shaula"]}))
    assert result["sources"]["journals@altair"] == {"status": "ok", "hits": 2}
    assert [h["id"] for h in result["related"]["journals@altair"]] == ["r1", "r2"]
    assert [h["id"] for h in result["related"]["journals@shaula"]] == ["r3"]
    remote = {h["id"]: h for h in every(result) if h["origin"]["node"] != HERE}
    assert remote["r1"]["origin"] == {"node": "altair"} and remote["r1"]["source"] == "journals"
    assert remote["r3"]["origin"] == {"node": "shaula"}
    assert "secret" not in remote["r1"]
    assert all(h["origin"]["node"] == HERE for hits in result["exact"].get("journals", []) for h in [hits])


async def test_only_the_named_node_is_asked_and_this_node_is_not_searched(loaded, peers):
    peers.add("shaula", peer_hits("r3"))
    peers.add("altair", peer_hits("r9"))
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals@shaula"]}))
    assert [h["id"] for h in every(result)] == ["r3"]
    assert set(result["sources"]) == {"journals@shaula"} and [c[0] for c in peers.calls] == ["shaula"]


async def test_a_node_is_asked_once_for_every_source_named_on_it(loaded, peers):
    peers.add("altair", peer_hits(sources=("journals",)))
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals@altair", "kb@altair"],
                                       "project": "awm", "limit": 4, "tags": "!closed"}))
    (name, tool, args), = peers.calls
    assert (name, tool) == ("altair", "search_query")
    assert args == {"query": "kitchen", "limit": 4, "project": "awm", "sources": ["journals", "kb"],
                    "tags": "!closed"}
    assert result["sources"]["kb@altair"] == {"status": "absent"}


async def test_a_node_without_an_edge_or_out_of_the_book_is_an_error_and_is_not_asked(loaded, peers):
    peers.add("shaula", peer_hits("r3"), edge=False)
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals", "journals@shaula",
                                                                        "journals@ghost"]}))
    assert result["sources"]["journals@shaula"] == {"status": "error", "error": f"no edge from {HERE} to shaula"}
    assert result["sources"]["journals@ghost"] == {"status": "error", "error": "not in the peer book"}
    assert peers.calls == [] and every(result)


async def test_a_station_is_never_asked(loaded, peers):
    peers.add("mira", peer_hits("r2"), role="station")
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals@mira"]}))
    assert result["sources"]["journals@mira"] == {"status": "station"} and peers.calls == []


async def test_a_node_without_the_search_tool_is_no_search(loaded, peers):
    peers.add("capella", lambda args: GatewayCallError(404, '{"detail":"Unknown tool: search_query"}'))
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals", "journals@capella"]}))
    assert result["sources"]["journals@capella"] == {"status": "no-search",
                                                     "detail": "its release has no search tool"}
    assert every(result)


async def test_a_failing_or_slow_node_is_a_status(loaded, peers, monkeypatch):
    monkeypatch.setattr(federation, "PEER_TIMEOUT_S", 0.05)

    async def slow(args):
        await asyncio.sleep(5)

    peers.add("down", lambda args: ConnectionError("refused"))
    peers.add("junk", lambda args: {"detail": "nope"})
    peers.add("big", lambda args: "x" * (federation.MAX_REPLY_BYTES + 1))
    peers.add("slow", slow)
    named = ["journals", *(f"journals@{n}" for n in ("down", "junk", "big", "slow"))]
    result = await loaded.query(parse({"query": "kitchen", "sources": named}))
    assert "refused" in result["sources"]["journals@down"]["error"]
    assert "no hits" in result["sources"]["journals@junk"]["error"]
    assert "exceeds" in result["sources"]["journals@big"]["error"]
    assert "timeout" in result["sources"]["journals@slow"]["error"]
    assert every(result)


async def test_a_call_from_a_peer_searches_this_node_only(loaded, peers):
    peers.add("altair", peer_hits("r1"))
    for as_ in ("peer:altair", "peer"):
        result = await loaded.query(parse({"query": "kitchen", "sources": ["journals", "kb@altair"]}, as_))
        assert result["sources"]["kb@altair"] == {"status": "absent"} and every(result)
    assert peers.calls == []


async def test_a_node_answering_with_one_list_per_source_keeps_each_source_apart(loaded, peers):
    peers.add("altair", lambda args: {
        "exact": {"journals": [{"source": "journals", "id": "far1", "title": "Kitchen elsewhere"}],
                  "kb": [{"source": "kb", "id": "doi:9", "title": "A far paper"}]},
        "related": {}, "graph": {},
        "sources": {"journals": {"status": "ok", "hits": 1}, "kb": {"status": "ok", "hits": 1}}})
    result = await loaded.query(parse({"query": "kitchen", "sources": ["journals", "journals@altair", "kb@altair"],
                                       "limit": 5}))
    assert [h["id"] for h in result["exact"]["journals@altair"]] == ["far1"]
    assert [h["id"] for h in result["exact"]["kb@altair"]] == ["doi:9"]
    assert "far1" not in {h["id"] for h in result["exact"]["journals"]}


async def test_get_with_a_node_reads_that_peers_archive(peers):
    peers.add("shaula", lambda args: {"document": {"title": "far", "text": "body", "origin": {"node": "liar"}}})
    got = await federation.fetch(peers, "shaula", "journals", "r3")
    assert got["document"]["origin"] == {"node": "shaula"}
    assert peers.calls == [("shaula", "search_get", {"source": "journals", "id": "r3"})]


# -- no reorder ----------------------------------------------------------------


async def test_no_source_is_reordered_by_kb(loaded, gateway):
    await loaded.query(parse({"query": "kitchen", "sources": LOADED}))
    assert not any(fn == "rank" for _, fn, _ in gateway.calls)


# -- the author filter --------------------------------------------


async def test_a_live_paper_source_and_a_peer_are_asked_with_the_author(loaded, gateway):
    asked = []
    gateway.plug_live("papers", lambda args: asked.append(args) or {"exact": [], "related": []},
                      service="papers")
    await loaded.refresh()
    q = parse({"query": "kitchen", "author": "Huttenhauer", "sources": ["papers"]})
    await loaded.query(q)
    assert asked[-1]["author"] == "Huttenhauer" and "then" not in asked[-1]
    assert q.forwarded(["papers"])["author"] == "Huttenhauer"


async def test_an_author_filter_leaves_the_archived_sources_empty(loaded):
    result = await loaded.query(parse({"query": "kitchen", "sources": LOADED, "author": "Lee"}))
    assert not every(result)
    assert result["sources"]["journals"] == {"status": "ok", "hits": 0}


async def test_the_library_is_a_live_source_of_its_own(loaded, gateway):
    gateway.plug_live("library", lambda args: {"exact": [], "related": [
        {"id": "doi:10.1/mine", "title": "My paper", "ts": "2021-01-01T00:00:00Z"}]}, service="kb")
    await loaded.refresh()
    both = await loaded.query(parse({"query": "kitchen tap", "sources": [*LOADED, "library"]}))
    assert [h["id"] for h in both["related"]["library"]] == ["doi:10.1/mine"]
    alone = await loaded.query(parse({"query": "kitchen tap", "sources": ["library"]}))
    assert set(alone["related"]) == {"library"} and not alone["exact"]


# -- get on a live source ---------------------------------------------------------


async def test_a_live_source_with_a_get_verb_reads_a_document_whole(loaded, gateway):
    gateway.plug_live("papers", lambda args: {"exact": [], "related": []}, service="papers", get="papers_get")
    gateway.handlers[("papers", "get")] = lambda args: {"document": {
        "title": "A paper", "text": "The whole paper.", "authors": ["Ann Lee"], "ts": "2021-01-01T00:00:00Z",
        "injected": True}}
    await loaded.refresh()
    got = (await loaded.get("papers", "doi:1"))["document"]
    assert got == {"title": "A paper", "text": "The whole paper.", "authors": ["Ann Lee"],
                   "ts": "2021-01-01T00:00:00Z", "source": "papers", "id": "doi:1", "frozen": False,
                   "origin": {"node": "local"}}
    assert gateway.calls[-1] == ("papers", "get", {"id": "doi:1"})


async def test_a_get_first_after_a_start_still_finds_a_live_source(loaded, gateway):
    gateway.plug_live("papers", lambda args: {"exact": [], "related": []}, service="papers", get="papers_get")
    gateway.handlers[("papers", "get")] = lambda args: {"document": {"title": "A paper", "text": "Whole."}}
    loaded.sources, loaded.registry_at = {}, None
    assert (await loaded.get("papers", "doi:1"))["document"]["text"] == "Whole."


async def test_a_live_source_without_a_get_verb_reads_nothing_whole(loaded, gateway):
    gateway.plug_live("web", transcripts)
    await loaded.refresh()
    with pytest.raises(ValueError, match="reads no document whole"):
        await loaded.get("web", "x")


async def test_a_get_naming_a_verb_the_service_lacks_is_an_error_source(loaded, gateway):
    gateway.plug_live("papers", lambda args: {"exact": [], "related": []}, service="papers", get="papers_get")
    gateway.entries[-1]["api"]["functions"] = [{"name": "query", "tool": "papers_query"}]
    await loaded.refresh()
    result = await loaded.query(parse({"query": "kitchen", "sources": ["papers"]}))
    assert "papers_get" in result["sources"]["papers"]["error"]


# -- tags on live sources ------------------------------------------------------------


async def test_a_tag_a_live_source_must_hold_answers_nothing_unasked(loaded, gateway):
    gateway.plug_live("web", transcripts)
    await loaded.refresh()
    held = await loaded.query(parse({"query": "kitchen", "sources": ["web", "journals"], "tags": "journal"}))
    assert held["sources"]["web"] == {"status": "ok", "hits": 0}
    assert not any(fn == "query" for _, fn, _ in gateway.calls)
    assert "j1" in {h["id"] for h in every(held)} and {h["id"] for h in every(held)} <= {"j1", "j2"}
    negated = await loaded.query(parse({"query": "kitchen", "sources": ["web"], "tags": "!closed"}))
    assert ids(negated) == [("web", "s1:7")]


# -- hop ----------------------------------------------------------------------------


async def test_a_reply_to_hop_walks_both_ways_from_the_ids_given(loaded):
    result = await loaded.query(parse({"sources": ["journals"], "hop": {"via": "reply_to", "from": ["j2"]}}))
    assert not flat(result["exact"]) and not flat(result["related"])
    assert {h["id"] for h in result["hop"]["journals"]} == {"j1", "j3"}
    assert all(h["match"] == "reply_to" for h in result["hop"]["journals"])
    assert result["sources"]["journals"]["hop"] == {"status": "ok", "hits": 2}


async def test_a_hop_without_from_starts_from_the_hits_for_the_words(loaded):
    result = await loaded.query(parse({"query": "garden shed roof", "sources": ["journals"],
                                       "hop": '{"via": "reply_to"}'}))
    assert [h["id"] for h in result["exact"]["journals"]] == ["j2"]
    assert {h["id"] for h in result["hop"]["journals"]} == {"j1", "j3"}


async def test_a_hop_obeys_the_filters(loaded):
    result = await loaded.query(parse({"sources": ["journals"], "tags": "!message",
                                       "hop": {"via": "reply_to", "from": ["j2"]}}))
    assert [h["id"] for h in result["hop"]["journals"]] == ["j1"]


@pytest.fixture()
async def papers(loaded, gateway):
    gateway.plug_live("papers", lambda args: {"exact": [{"id": "doi:2", "title": "B"}, {"id": "doi:1", "title": "A"}],
                                              "related": [{"id": "doi:3", "title": "C"}]},
                      hop="papers_hop", links=["citations", "cited_by", "authors"])
    walked = []
    gateway.handlers[("papers", "hop")] = lambda args: walked.append(args) or {"hits": [
        {"id": "doi:9", "title": "Cited", "kind": "paper", "match": "cites", "score": 0.4, "text": "no"},
        {"id": "doi:8", "title": "Also cited"}]}
    await loaded.refresh()
    return walked


async def test_a_paper_hop_seeds_from_the_exact_hits_in_order_and_keeps_the_walks_order(loaded, papers):
    result = await loaded.query(parse({"query": "kitchen", "sources": ["papers", "journals"],
                                       "after": "2020-01-01", "hop": {"via": "citations", "limit": 5}}))
    assert papers == [{"ids": ["doi:2", "doi:1"], "along": "cites", "limit": 5, "after": "2020-01-01"}]
    assert [h["id"] for h in result["hop"]["papers"]] == ["doi:9", "doi:8"]
    assert "text" not in result["hop"]["papers"][0]
    assert result["sources"]["journals"]["hop"] == {"status": "none", "detail": "no citations links"}


async def test_a_paper_hop_from_ids_needs_no_words(loaded, papers, gateway):
    result = await loaded.query(parse({"sources": ["papers"], "hop": {"via": "authors", "from": ["doi:7"]}}))
    assert papers[-1]["ids"] == ["doi:7"] and papers[-1]["along"] == "authors"
    assert not any(fn == "query" for _, fn, _ in gateway.calls)
    assert [h["id"] for h in result["hop"]["papers"]] == ["doi:9", "doi:8"]


async def test_a_failing_hop_is_a_status_and_never_fails_the_search(loaded, papers, gateway):
    gateway.handlers[("papers", "hop")] = lambda args: RuntimeError("no hop arrays")
    result = await loaded.query(parse({"query": "kitchen", "sources": ["papers"], "hop": {"via": "cited_by"}}))
    assert "no hop arrays" in result["sources"]["papers"]["hop"]["error"]
    assert ids(result)


@pytest.mark.parametrize("hop, match", [
    ({"via": "friends"}, "via"), ({"via": "citations", "depth": 2}, "unknown keys"), ("[1]", "JSON object"),
    ("{not json", "hop:"),
])
def test_a_bad_hop_is_refused(hop, match):
    with pytest.raises(ValueError, match=match):
        parse({"query": "x", "sources": ["kb"], "hop": hop})


async def test_a_hop_goes_to_the_node_that_holds_the_source(loaded, peers):
    peers.add("altair", lambda args: {"exact": [], "related": [],
                                      "hop": {"kb": [{"source": "kb", "id": "doi:5", "title": "Far"}]},
                                      "sources": {"kb": {"status": "ok", "hits": 0,
                                                         "hop": {"status": "ok", "hits": 1}}}})
    result = await loaded.query(parse({"sources": ["kb@altair"], "hop": {"via": "citations", "from": ["doi:1"]}}))
    assert peers.calls[0][2]["hop"] == {"via": "citations", "from": ["doi:1"], "limit": 20}
    assert [h["id"] for h in result["hop"]["kb@altair"]] == ["doi:5"]
    assert result["sources"]["kb@altair"] == {"status": "ok", "hits": 0, "hop": {"status": "ok", "hits": 1}}
