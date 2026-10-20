"""`kb query`'s meaning and graph legs, and the graph, schema and related verbs, with the kb server mocked."""

import httpx
import pytest

from awm.kb import client, fastlayer, hub_adapter, semantic

from .test_fastlayer import BODY, _base

PAPERS = {
    "doi:10.1/at": ("10.1/at", "Assembly theory explains and quantifies selection and evolution", 2023,
                    "We introduce assembly theory.", BODY),
    "doi:10.1/cas": ("10.1/cas", "A programmable dual-RNA-guided DNA endonuclease", 2012,
                     "Cas9 cleaves DNA guided by RNA.", "The PAM is required on double-stranded DNA."),
    "doi:10.1/old": ("10.1/old", "An ASSEMBLY INDEX survey from long ago", 1999,
                     "A survey of the assembly index.", ""),
    "doi:10.1/lone": ("10.1/lone", "Constructive molecular lineages", 2020, "Lineages of molecules.", ""),
}


def entry(doc, zone, score, start=0, end=10, source="paper", part=None):
    return {"score": score, "gid": hash((doc, zone, start)) % 10**6, "zone": zone, "source": source, "doc": doc,
            "part": part or zone, "start": start, "end": end}


@pytest.fixture
def layer(tmp_path, monkeypatch):
    _base(tmp_path / "papers.db", PAPERS)
    layer = fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "papers_delta.db")
    monkeypatch.setattr(hub_adapter, "PAPERS", layer)
    return layer


class Server:
    """Stands in for the kb server's /related and /graph."""

    def __init__(self):
        self.entries = []
        self.nouns = []
        self.related_error = None
        self.graph_rows = {}
        self.graph_error = None
        self.related_bodies = []
        self.graph_bodies = []

    async def related(self, body, **kw):
        self.related_bodies.append(body)
        if self.related_error:
            raise self.related_error
        return {"hits": self.entries, "nouns": self.nouns, "generation": "gen-0001"}

    async def graph(self, body, **kw):
        self.graph_bodies.append(body)
        if self.graph_error:
            raise self.graph_error
        for label, rows in self.graph_rows.items():
            if label in body["cypher"]:
                return {"columns": [], "rows": rows, "truncated": False, "bound": {}}
        return {"columns": [], "rows": [], "truncated": False, "bound": {}}


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(client, "related", s.related)
    monkeypatch.setattr(client, "graph", s.graph)
    return s


async def query(**args):
    return await hub_adapter.HANDLERS["query"]({"query": "molecular joining steps lineages", **args})


async def test_each_zones_best_papers_are_kept_and_merged(layer, server):
    server.entries = [
        entry("doi:10.1/at", "abstract", 0.90), entry("doi:10.1/cas", "abstract", 0.80),
        entry("doi:10.1/old", "title", 0.88), entry("doi:10.1/lone", "title", 0.70),
        entry("doi:10.1/lone", "body", 0.86, 1000, 1010), entry("doi:10.1/cas", "body", 0.60, 1000, 1010)]
    got = await query(limit=10)
    ids = [h["id"] for h in got["related"]]
    assert set(ids) == {"doi:10.1/at", "doi:10.1/cas", "doi:10.1/old", "doi:10.1/lone"}
    assert {h["match"] for h in got["related"]} == {"meaning"}
    assert got["semantic"] == {"status": "ok", "generation": "gen-0001"} and "degraded" not in got
    assert server.related_bodies[0]["zones"] == ["body", "abstract", "title"] and server.related_bodies[0]["nouns"]


async def test_a_paper_strong_in_two_zones_beats_one_strong_in_one(layer, server):
    server.entries = [
        entry("doi:10.1/lone", "abstract", 0.9), entry("doi:10.1/at", "abstract", 0.8),
        entry("doi:10.1/at", "title", 0.9), entry("doi:10.1/old", "title", 0.85)]
    got = await query(limit=3)
    assert [h["id"] for h in got["related"]][0] == "doi:10.1/at"


async def test_a_hit_quotes_the_passage_its_entry_overlaps(layer, server):
    server.entries = [entry("doi:10.1/at", "body", 0.9, 1020, 1060),
                      entry("doi:10.1/at", "abstract", 0.8, 0, 28)]
    got = await query(passages=2)
    quotes = got["related"][0]["passages"]
    assert [(q["field"], q["start"]) for q in quotes] == [("body", 1000), ("abstract", 0)]
    assert quotes[0]["text"] == BODY and quotes[0]["section"] == "Results"
    assert got["related"][0]["url"] == "https://doi.org/10.1/at"


async def test_an_entry_in_no_passage_quotes_nothing_and_the_hit_stays(layer, server):
    server.entries = [entry("doi:10.1/at", "body", 0.9, 90000, 90010)]
    got = await query(query="zzqq")
    assert [(h["id"], h["passages"]) for h in got["related"]] == [("doi:10.1/at", [])]


async def test_exact_papers_are_not_repeated_in_related_and_keyword_matches_join_in(layer, server):
    server.entries = [entry("doi:10.1/at", "abstract", 0.9), entry("doi:10.1/lone", "abstract", 0.5)]
    got = await query(query="shortest construction path")
    assert [h["id"] for h in got["exact"]] == ["doi:10.1/at"]
    assert "doi:10.1/at" not in [h["id"] for h in got["related"]]
    only_words = await query(query="assembly index of a molecule and its DNA")
    kinds = {h["id"]: h["match"] for h in only_words["related"]}
    assert kinds["doi:10.1/at"] == "meaning" and kinds["doi:10.1/lone"] == "meaning"


async def test_the_date_filters_apply_to_meaning_matches(layer, server):
    server.entries = [entry("doi:10.1/at", "abstract", 0.9), entry("doi:10.1/cas", "abstract", 0.8)]
    got = await query(after="2020-01-01", before="2030-01-01")
    assert [h["id"] for h in got["related"]] == ["doi:10.1/at"]


async def test_archive_entries_and_unknown_papers_are_not_papers(layer, server):
    server.entries = [entry("j1", "journal", 0.99, source="journals", part="text"),
                      entry("doi:10.1/ghost", "abstract", 0.95), entry("doi:10.1/lone", "abstract", 0.5)]
    got = await query(query="zzqq")
    assert [h["id"] for h in got["related"]] == ["doi:10.1/lone"]


@pytest.mark.parametrize("error", [
    client.KbError(503, "encoder is loading"),
    client.KbError(503, "no index under /w2/vectors/entries"),
    httpx.ConnectError("refused")])
async def test_without_the_server_encoder_or_index_it_answers_keywords_and_says_so(layer, server, error):
    server.related_error = error
    got = await query(query="assembly index of a molecule and its DNA")
    assert [h["id"] for h in got["related"]] == ["doi:10.1/at"] and got["related"][0]["match"] == "keyword"
    assert got["graph"] == [] and got["semantic"]["status"] == "unavailable"
    assert "keyword only" in got["degraded"]["semantic"]
    if isinstance(error, client.KbError):
        assert error.detail in got["degraded"]["semantic"]
    assert server.graph_bodies == []


async def test_a_project_filter_never_reaches_the_server(layer, server):
    got = await query(project="awm")
    assert got["exact"] == got["related"] == got["graph"] == [] and server.related_bodies == []


async def test_graph_neighbours_are_ordered_by_pagerank_and_skip_what_is_shown(layer, server):
    server.entries = [entry("doi:10.1/at", "abstract", 0.9), entry("doi:10.1/lone", "abstract", 0.5)]
    server.nouns = [{"score": 0.8, "gid": 1, "name": "assembly", "type": "Entity", "node_ids": ["e1", "e2"]},
                    {"score": 0.7, "gid": 2, "name": "index", "type": "Entity", "node_ids": ["e2", "e3"]}]
    server.graph_rows = {
        "CITES": [["doi:10.1/old", "An ASSEMBLY INDEX survey from long ago", 1999, 0.01],
                  ["doi:10.1/lone", "Constructive molecular lineages", 2020, 0.9]],
        "AUTHORED": [["doi:10.1/cas", "A programmable dual-RNA-guided DNA endonuclease", 2012, 0.05],
                     ["doi:10.1/old", "An ASSEMBLY INDEX survey from long ago", 1999, 0.01]],
        "HAS_CHUNK": [["doi:10.1/ghost", "Not in papers.db", 2000, 0.99]]}
    got = await query(limit=5)
    graph = got["graph"]
    assert [h["id"] for h in graph] == ["doi:10.1/cas", "doi:10.1/old"]
    assert [h["match"] for h in graph] == ["shares an author", "cited-by or cites"]
    assert graph[0]["score"] > graph[1]["score"] and graph[0]["passages"][0]["field"] == "abstract"
    by_label = {("CITES" if "CITES" in b["cypher"] else "AUTHORED" if "AUTHORED" in b["cypher"] else "HAS_CHUNK"): b
                for b in server.graph_bodies}
    assert by_label["CITES"]["params"]["seeds"][:2] == ["paper:doi:10.1/at", "paper:doi:10.1/lone"]
    assert by_label["HAS_CHUNK"]["params"]["ents"] == ["e1", "e2", "e3"]
    assert all(b["timeout_s"] <= 5 for b in server.graph_bodies)


async def test_a_failing_graph_leaves_the_other_sections_and_says_why(layer, server):
    server.entries = [entry("doi:10.1/at", "abstract", 0.9)]
    server.graph_error = client.KbError(503, "no graph at /w2/graph/graph.ladybug")
    got = await query()
    assert [h["id"] for h in got["related"]] == ["doi:10.1/at"] and got["graph"] == []
    assert "no graph" in got["degraded"]["graph"] and "semantic" not in got["degraded"]


def test_zone_union_keeps_a_papers_best_entry_in_each_zone():
    entries = [entry("a", "body", 0.5), entry("a", "body", 0.9), entry("b", "body", 0.7), entry("c", "title", 0.4)]
    assert semantic.zone_lists(entries, 5) == {"body": ["a", "b"], "abstract": [], "title": ["c"]}
    assert semantic.zone_lists(entries, 1)["body"] == ["a"]


def test_merge_sums_reciprocal_ranks():
    merged = semantic.merge([["a", "b"], ["b", "c"], []])
    assert [p for p, _ in merged] == ["b", "a", "c"]


# -- verbs ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def relayed(monkeypatch):
    seen = []

    async def graph(body, **kw):
        seen.append(("graph", body, kw))
        return {"columns": ["n"], "rows": [[1]], "truncated": False, "bound": {}}

    async def schema():
        seen.append(("schema",))
        return {"graph": {"nodes": {"Paper": {}}}}

    async def related(body, **kw):
        seen.append(("related", body))
        return {"hits": []}

    monkeypatch.setattr(client, "graph", graph)
    monkeypatch.setattr(client, "schema", schema)
    monkeypatch.setattr(client, "related", related)
    monkeypatch.setattr(hub_adapter.config, "peer_relation", lambda n: "foreign" if n == "stranger" else "domestic")
    monkeypatch.setattr(hub_adapter.config, "caller_peer", lambda as_: (as_ or "").removeprefix("peer:") or None)
    return seen


async def test_graph_relays_the_query_near_and_limits(relayed):
    args = {"cypher": "MATCH (p:Paper) RETURN p LIMIT 1", "near": {"p": {"index": "entries", "text": "x"}},
            "params": {"y": 1}, "limit": 5, "timeout_s": 3}
    got = await hub_adapter.HANDLERS["graph"](args)
    assert got["rows"] == [[1]]
    assert relayed[0][1] == args
    assert relayed[0][2]["timeout"] > 3


async def test_graph_needs_a_query_and_a_domestic_caller(relayed):
    with pytest.raises(ValueError):
        await hub_adapter.HANDLERS["graph"]({"cypher": "  "})
    with pytest.raises(PermissionError):
        await hub_adapter.HANDLERS["graph"]({"cypher": "MATCH (n) RETURN n"}, as_="peer:stranger")
    assert relayed == []
    await hub_adapter.HANDLERS["graph"]({"cypher": "MATCH (n) RETURN n"}, as_="peer:mira")
    assert len(relayed) == 1


async def test_schema_is_relayed_and_related_is_domestic(relayed):
    assert (await hub_adapter.HANDLERS["schema"]({}, as_="peer:stranger"))["graph"]["nodes"] == {"Paper": {}}
    with pytest.raises(PermissionError):
        await hub_adapter.HANDLERS["related"]({"text": "x"}, as_="peer:stranger")
    await hub_adapter.HANDLERS["related"]({"text": "x", "zones": ["journal"], "junk": 1})
    assert relayed[-1] == ("related", {"text": "x", "zones": ["journal"]})


def test_the_manifest_declares_the_new_verbs_under_the_kb_grant():
    verbs = {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}
    for name in ("graph", "schema", "related"):
        assert verbs[name]["category"] == "kb" and verbs[name]["effect"] == "read"
    assert "surfaces" not in verbs["graph"] and verbs["related"]["surfaces"] == ["cli", "http"]
    assert set(hub_adapter.HANDLERS) >= set(verbs)
