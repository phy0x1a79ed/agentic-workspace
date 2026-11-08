"""The paper fast layer: exact and keyword paper hits from base and delta, and the Zotero delta's map."""

import sqlite3

import pytest

from awm.kb import fastlayer, hub_adapter

BODY = ("Assembly theory quantifies selection. The assembly index of a molecule counts the joining steps "
        "on its shortest construction path.")


def _base(path, papers, schema=fastlayer.SCHEMA):
    """A fresh base with the shared schema; ``papers`` maps an id to (doi, title, year, abstract, body)."""
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(fastlayer.DELTA_DDL)
    db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(schema),))
    for pid, (doi, title, year, abstract, body) in papers.items():
        db.execute("INSERT INTO paper(paper_id, doi, title, year, ts) VALUES (?,?,?,?,?)",
                   (pid, doi, title, year, f"{year}-01-01T00:00:00Z"))
        for zone, text in (("title", title), ("abstract", abstract)):
            db.execute("INSERT INTO passage(paper_id, zone, ord, start, end, text) VALUES (?,?,0,0,?,?)",
                       (pid, zone, len(text), text))
        if body:
            db.execute("INSERT INTO passage(paper_id, zone, ord, start, end, section, text) VALUES (?,?,0,?,?,?,?)",
                       (pid, "body", 1000, 1000 + len(body), "Results", body))
    db.commit()
    db.close()


@pytest.fixture
def layer(tmp_path):
    _base(tmp_path / "papers.db", {
        "doi:10.1/at": ("10.1/at", "Assembly theory explains and quantifies selection and evolution", 2023,
                        "We introduce assembly theory.", BODY),
        "doi:10.1/cas": ("10.1/cas", "A programmable dual-RNA-guided DNA endonuclease", 2012,
                         "Cas9 cleaves DNA guided by RNA.", "The PAM is required on double-stranded DNA."),
        "doi:10.1/old": ("10.1/old", "An ASSEMBLY INDEX survey from long ago", 1999, "", ""),
    })
    return fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "papers_delta.db")


def exact(layer, *args):
    return layer.search(*args)["exact"]


def test_papers_holding_only_some_words_are_related_not_exact(layer):
    found = layer.search("assembly index of a molecule and its DNA", fastlayer.Filters(), 10)
    assert found["exact"] == []
    assert [(h["id"], h["match"]) for h in found["related"]] == [("doi:10.1/at", "keyword")]
    both = layer.search("double-stranded DNA", fastlayer.Filters(), 10)
    assert [h["id"] for h in both["exact"]] == ["doi:10.1/cas"] and both["related"] == []


def test_a_literal_in_a_body_finds_its_paper_and_quotes_none_of_it(layer):
    hits = exact(layer, "shortest construction path", fastlayer.Filters(), 10)
    assert [(h["id"], h["match"]) for h in hits] == [("doi:10.1/at", "literal")]
    assert "passages" not in hits[0]
    assert hits[0]["url"] == "https://doi.org/10.1/at" and hits[0]["kind"] == "paper"


def test_a_literal_with_like_wildcards_matches_only_itself(tmp_path):
    _base(tmp_path / "papers.db", {
        "a": ("", "Clustering at 100% identity with p_value cut", 2020, "", ""),
        "b": ("", "Clustering at 100 x identity with pXvalue cut", 2020, "", "")})
    layer = fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "delta.db")
    for needle in ("100% identity", "p_value"):
        assert [h["id"] for h in exact(layer, needle, fastlayer.Filters(), 10)
                if h["match"] == "literal"] == ["a"], needle


def test_a_base_of_another_schema_is_not_read(tmp_path):
    _base(tmp_path / "papers.db", {"p": ("", "Assembly theory", 2023, "", "")}, schema=99)
    layer = fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "delta.db")
    assert exact(layer, "Assembly", fastlayer.Filters(), 10) == []


def test_zotero_items_map_onto_the_base_or_wait_in_the_delta(layer, tmp_path):
    items = [
        {"key": "AAA", "library": "users/0", "version": 5, "doi": "10.1/AT", "title": "whatever"},
        {"key": "BBB", "library": "users/0", "version": 6, "title": "A programmable dual-RNA-guided DNA endonuclease",
         "year": "2012"},
        {"key": "CCC", "library": "users/0", "version": 7, "title": "Microbial mats of the hydrothermal vent field",
         "year": 2024, "abstract": "Vent mats host iron oxidisers.", "creators": ["Smith"]},
    ]
    assert layer.take_items(items, {"users/0": 7}) == {"mapped": 2, "held": 1}
    assert layer.cursor() == {"users/0": 7}
    hits = exact(layer, "iron oxidisers", fastlayer.Filters(), 10)
    assert [h["id"] for h in hits] == ["zotero:users/0/CCC"]
    assert layer.status()["delta"]["library"] == 3

    _base(tmp_path / "papers.db", {"doi:10.1/vent": ("", "Microbial mats of the hydrothermal vent field", 2024,
                                                      "Vent mats host iron oxidisers.", "")})
    assert layer.remap() == {"remapped": 1}
    assert [h["id"] for h in exact(layer, "iron oxidisers", fastlayer.Filters(), 10)] == ["doi:10.1/vent"]


def _crowd(path, n=120, seed=3):
    """A base of ``n`` papers whose bodies draw words from a small vocabulary, so most words are common."""
    import random

    rnd = random.Random(seed)
    vocab = [f"{p}{s}" for p in ("alp", "bet", "gam", "del", "eps", "zet", "eta", "the") for s in ("ron", "tal", "mix", "ing")]
    papers = {}
    for i in range(n):
        body = " ".join(rnd.choice(vocab) for _ in range(40)) + f". Paper {i} ends, with marker qq{i:03d}x."
        papers[f"p{i:03d}"] = ("", f"Title of {rnd.choice(vocab)} {i}", 2000 + i % 20, " ".join(rnd.choice(vocab) for _ in range(15)), body)
    _base(path, papers)
    return papers


def test_every_substring_of_a_passage_finds_its_paper(tmp_path):
    papers = _crowd(tmp_path / "papers.db")
    layer = fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "delta.db")
    body = papers["p007"][4]
    for length in (3, 4, 7, 12, 13, 18, 30, 55, 90, len(body)):
        for start in range(0, len(body) - length + 1, 17):
            needle = body[start:start + length].strip()
            if len(needle) < 3:
                continue
            hits = layer.search(needle, fastlayer.Filters(), 1000)["exact"]
            assert "p007" in [h["id"] for h in hits], (start, length, needle)


def test_a_common_phrase_still_finds_the_one_paper_past_the_candidate_cap(tmp_path, monkeypatch):
    papers = _crowd(tmp_path / "papers.db")
    monkeypatch.setattr(fastlayer, "CANDIDATES", 8)
    layer = fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "delta.db")
    needle = " ends, with marker qq099x."
    assert [h["id"] for h in layer.search(needle, fastlayer.Filters(), 100)["exact"]] == ["p099"]


def test_ranges_of_the_index_rank_as_the_whole_does(tmp_path, monkeypatch):
    _crowd(tmp_path / "papers.db")
    layer = fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "delta.db")
    queries = ["alpron betmix", "thetal gamron deling epsmix", "zetron", "etatal delmix alpron"]
    whole = [layer.search(q, fastlayer.Filters(), 15) for q in queries]
    monkeypatch.setattr(fastlayer, "SHARD_PASSAGES", 1)
    assert [layer.search(q, fastlayer.Filters(), 15) for q in queries] == whole


def test_counts_cached_for_a_store_do_not_outlive_it(tmp_path, monkeypatch):
    monkeypatch.setattr(fastlayer, "DF_CACHED", 1)
    _base(tmp_path / "papers.db", {"a": ("", "Zymurgy of beer", 2020, "", "")})
    layer = fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "delta.db")
    assert [h["id"] for h in layer.search("zymurgy beer", fastlayer.Filters(), 5)["exact"]] == ["a"]
    _base(tmp_path / "papers.db", {"b": ("", "Zymurgy of wine", 2021, "", ""), "c": ("", "Zymurgy of beer too", 2022, "", "")})
    assert {h["id"] for h in layer.search("zymurgy", fastlayer.Filters(), 5)["exact"]} == {"b", "c"}


async def test_a_poll_takes_every_page_and_stores_the_cursor_last(layer, monkeypatch):
    pages = {0: {"items": [{"key": "X1", "library": "users/0", "version": 1, "title": "T one is long enough here",
                            "year": 2020}], "versions": {"users/0": 9}, "next": 1},
             1: {"items": [{"key": "X2", "library": "users/0", "version": 9, "title": "T two is long enough here",
                            "year": 2021}], "versions": {"users/0": 9}, "next": None}}
    asked = []

    async def page(args):
        asked.append(args)
        assert layer.cursor() == {}
        return pages[args["offset"]]

    monkeypatch.setattr(hub_adapter, "PAPERS", layer)
    monkeypatch.setattr(hub_adapter, "_zotero_page", page)
    assert await hub_adapter.zotero_poll() == {"mapped": 0, "held": 2}
    assert [a["offset"] for a in asked] == [0, 1] and layer.cursor() == {"users/0": 9}


CHAMPION_DDL = """
CREATE TABLE term_df(term TEXT PRIMARY KEY, doc INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE champion(term TEXT NOT NULL, score REAL NOT NULL, passage INTEGER NOT NULL,
                      PRIMARY KEY (term, score, passage)) WITHOUT ROWID;
"""


def _champion(path, depth):
    """Give a base kb's schema-4 champion lists, as kb.fastlayer builds them for stems porter keeps."""
    db = sqlite3.connect(path)
    db.executescript(CHAMPION_DDL)
    db.execute("INSERT INTO term_df SELECT term, doc FROM passage_vocab")
    for (term,) in db.execute("SELECT term FROM term_df").fetchall():
        db.executemany("INSERT INTO champion VALUES (?, ?, ?)", [(term, r, i) for i, r in db.execute(
            "SELECT rowid, rank FROM passage_bm25 WHERE passage_bm25 MATCH ? ORDER BY rank LIMIT ?",
            (f'"{term}"', depth))])
    db.execute("INSERT INTO meta VALUES ('champion_depth', ?)", (str(depth),))
    db.commit()
    db.close()


PAPERS = {
    "doi:10.1/at": ("10.1/at", "Assembly theory explains and quantifies selection and evolution", 2023,
                    "We introduce assembly theory.", BODY),
    "doi:10.1/cas": ("10.1/cas", "A programmable dual-RNA-guided DNA endonuclease", 2012,
                     "Cas9 cleaves DNA guided by RNA.", "The PAM is required on double-stranded DNA."),
    "doi:10.1/old": ("10.1/old", "An ASSEMBLY INDEX survey from long ago", 1999, "", ""),
    "doi:10.1/mol": ("10.1/mol", "Molecule selection in prebiotic chemistry", 2020,
                     "Selection acts on each molecule before life.", "Index of joining steps in chemistry."),
}
QUERIES = ["assembly index of a molecule and its DNA", "double-stranded DNA", "RNA guided cutting of DNA",
           "selection of a molecule", "joining steps chemistry", "assembly index"]


def _answers(layer):
    return [{k: [(h["id"], h["match"]) for h in v] for k, v in layer.search(q, fastlayer.Filters(), 10).items()}
            for q in QUERIES]


def test_complete_champion_lists_answer_as_fts5s_own_ranking_does(tmp_path):
    _base(tmp_path / "ranked.db", PAPERS)
    _base(tmp_path / "champion.db", PAPERS)
    _champion(tmp_path / "champion.db", depth=1000)
    ranked = fastlayer.FastLayer(tmp_path / "ranked.db", tmp_path / "d1.db")
    champion = fastlayer.FastLayer(tmp_path / "champion.db", tmp_path / "d2.db")
    assert fastlayer._champions(tmp_path / "champion.db") and not fastlayer._champions(tmp_path / "ranked.db")
    assert _answers(champion) == _answers(ranked)


def test_the_delta_is_ranked_exactly_beside_a_champion_base(tmp_path):
    _base(tmp_path / "papers.db", PAPERS)
    _champion(tmp_path / "papers.db", depth=1000)
    layer = fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "delta.db")
    layer.take_items([{"key": "Z9", "library": "users/0", "version": 1, "year": "2024",
                       "title": "Hydrothermal vent molecule selection", "abstract": "Vent chemistry selects."}])
    got = layer.search("vent molecule selection", fastlayer.Filters(), 10)
    ids = [h["id"] for h in got["exact"] + got["related"]]
    assert "zotero:users/0/Z9" in ids and "doi:10.1/mol" in ids


def test_a_library_search_reads_the_library_copy_and_follows_the_library(layer):
    layer.take_items([{"library": "u", "key": "K1", "version": 1, "doi": "10.1/at", "title": "Assembly theory"}])
    mine = hub_adapter.fastlayer.Filters(papers=layer.library())
    got = layer.search_library("assembly", mine, 10)
    assert [h["id"] for h in got["exact"]] == ["doi:10.1/at"]
    copy = sqlite3.connect(layer.library_path)
    assert copy.execute("SELECT count(*) FROM paper").fetchone()[0] == 1
    layer.take_items([{"library": "u", "key": "K2", "version": 2, "doi": "10.1/old", "title": "An ASSEMBLY INDEX survey"}])
    got = layer.search_library("assembly", fastlayer.Filters(papers=layer.library()), 10)
    assert {h["id"] for h in got["exact"]} == {"doi:10.1/at", "doi:10.1/old"}
