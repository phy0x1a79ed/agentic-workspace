"""`kb query`'s meaning leg, `kb hop`'s hits and the related verb, with the kb server mocked."""

import sqlite3

import httpx
import pytest

from awm.kb import client, fastlayer, hops, hub_adapter, semantic

from .test_fastlayer import BODY, _base
from .test_hops import write_hops

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
    """Stands in for the kb server's /related."""

    def __init__(self):
        self.entries = []
        self.nouns = []
        self.related_error = None
        self.related_bodies = []

    async def related(self, body, **kw):
        self.related_bodies.append(body)
        if self.related_error:
            raise self.related_error
        return {"hits": self.entries, "nouns": self.nouns, "generation": "gen-0001"}


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(client, "related", s.related)
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


async def test_library_papers_answer_as_their_own_source_and_get_no_boost_in_kb(layer, server):
    server.entries = [entry("doi:10.1/at", "abstract", 0.9), entry("doi:10.1/cas", "abstract", 0.8),
                      entry("doi:10.1/old", "abstract", 0.7)]
    conn = layer._delta()
    conn.execute("INSERT INTO library(zotero_key, paper_id, version) VALUES ('u/K1', 'doi:10.1/old', 1)")
    conn.commit()
    conn.close()
    hub_adapter._answers.clear()
    assert [h["id"] for h in (await query(limit=3))["related"]] == ["doi:10.1/at", "doi:10.1/cas", "doi:10.1/old"]
    mine = await hub_adapter.HANDLERS["library"]({"query": "molecular joining steps lineages", "limit": 3})
    assert [h["id"] for h in mine["related"]] == ["doi:10.1/old"] and "graph" not in mine
    assert len(server.related_bodies) == 1  # one meaning lookup served both sources


async def test_a_hit_carries_its_title_date_and_url_and_no_passages(layer, server):
    server.entries = [entry("doi:10.1/at", "body", 0.9, 1020, 1060),
                      entry("doi:10.1/at", "abstract", 0.8, 0, 28)]
    got = await query()
    hit = got["related"][0]
    assert hit["id"] == "doi:10.1/at" and hit["url"] == "https://doi.org/10.1/at" and hit["title"].startswith("Assembly")
    assert not any("passages" in h for h in [*got["exact"], *got["related"]])


async def test_an_entry_in_no_passage_keeps_the_hit(layer, server):
    server.entries = [entry("doi:10.1/at", "body", 0.9, 90000, 90010)]
    got = await query(query="zzqq")
    assert [h["id"] for h in got["related"]] == ["doi:10.1/at"]


async def test_kb_get_reads_a_paper_whole_and_refuses_one_it_lacks(layer):
    got = (await hub_adapter.HANDLERS["get"]({"id": "doi:10.1/at"}))["document"]
    assert got["title"].startswith("Assembly theory") and got["url"] == "https://doi.org/10.1/at"
    assert got["text"].startswith("We introduce assembly theory.") and BODY in got["text"]
    with pytest.raises(ValueError, match="no paper"):
        await hub_adapter.HANDLERS["get"]({"id": "doi:10.1/none"})


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
    assert "graph" not in got and got["semantic"]["status"] == "unavailable"
    assert "keyword only" in got["degraded"]["semantic"]
    if isinstance(error, client.KbError):
        assert error.detail in got["degraded"]["semantic"]


async def test_a_hop_paper_with_no_abstract_is_still_a_hit(layer, server, tmp_path, monkeypatch):
    db = sqlite3.connect(layer.base_path)
    db.execute("DELETE FROM passage WHERE paper_id = 'doi:10.1/old' AND zone = 'abstract'")
    db.commit()
    db.close()
    write_hops(tmp_path / "hops", list(PAPERS), cites=[("doi:10.1/at", "doi:10.1/old")])
    monkeypatch.setattr(hub_adapter, "HOPS", hops.Hops(tmp_path / "hops"))
    graph = (await hub_adapter.HANDLERS["hop"]({"ids": ["doi:10.1/at"], "along": "cites"}))["hits"]
    assert [(h["id"], h["title"]) for h in graph] == [("doi:10.1/old", "An ASSEMBLY INDEX survey from long ago")]
    assert "passages" not in graph[0]


async def test_a_project_filter_never_reaches_the_server(layer, server):
    got = await query(project="awm")
    assert got["exact"] == got["related"] == [] and server.related_bodies == []


async def test_without_hop_arrays_a_query_answers_and_a_hop_says_why_not(layer, server, tmp_path, monkeypatch):
    monkeypatch.setattr(hub_adapter, "HOPS", hops.Hops(tmp_path / "nowhere"))
    server.entries = [entry("doi:10.1/at", "abstract", 0.9)]
    got = await query()
    assert [h["id"] for h in got["related"]] == ["doi:10.1/at"]
    with pytest.raises(RuntimeError, match="no hop arrays"):
        await hub_adapter.HANDLERS["hop"]({"ids": ["doi:10.1/at"]})
    with pytest.raises(ValueError, match="hop arrays"):
        await query(author="Cronin")


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

    async def related(body, **kw):
        seen.append(("related", body))
        return {"hits": []}

    monkeypatch.setattr(client, "related", related)
    monkeypatch.setattr(hub_adapter.config, "caller_peer", lambda as_: (as_ or "").removeprefix("peer:") or None)
    return seen


async def test_related_serves_this_node_only(relayed):
    with pytest.raises(PermissionError):
        await hub_adapter.HANDLERS["related"]({"text": "x"}, as_="peer:home")
    await hub_adapter.HANDLERS["related"]({"text": "x", "zones": ["journal"], "junk": 1})
    assert relayed[-1] == ("related", {"text": "x", "zones": ["journal"]})


def test_the_manifest_declares_related_as_a_read_off_the_mcp_surface():
    verbs = {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}
    assert "category" not in verbs["related"] and verbs["related"]["effect"] == "read"
    assert verbs["related"]["surfaces"] == ["cli", "http"]
    assert set(hub_adapter.HANDLERS) >= set(verbs)
