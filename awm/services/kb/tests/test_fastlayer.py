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
    found = layer.search("assembly index of a molecule and its DNA", fastlayer.Filters(), 10, 1)
    assert found["exact"] == []
    assert [(h["id"], h["match"]) for h in found["related"]] == [("doi:10.1/at", "keyword")]
    both = layer.search("double-stranded DNA", fastlayer.Filters(), 10, 1)
    assert [h["id"] for h in both["exact"]] == ["doi:10.1/cas"] and both["related"] == []


def test_a_literal_in_a_body_quotes_its_passage_with_offsets(layer):
    hits = exact(layer, "shortest construction path", fastlayer.Filters(), 10, 1)
    assert [(h["id"], h["match"]) for h in hits] == [("doi:10.1/at", "literal")]
    quote = hits[0]["passages"][0]
    assert (quote["field"], quote["section"], quote["start"]) == ("body", "Results", 1000)
    start, end = quote["marks"][0]
    assert BODY[start - 1000:end - 1000] == "shortest construction path"
    assert hits[0]["url"] == "https://doi.org/10.1/at" and hits[0]["kind"] == "paper"


def test_a_sentence_long_literal_is_probed_by_its_rarest_trigrams(layer):
    sentence = "The assembly index of a molecule counts the joining steps on its shortest"
    hits = exact(layer, sentence, fastlayer.Filters(), 10, 1)
    assert [(h["id"], h["match"]) for h in hits if h["match"] == "literal"] == [("doi:10.1/at", "literal")]
    assert exact(layer, sentence + " zzqx", fastlayer.Filters(), 10, 1) == [] or \
        all(h["match"] == "keyword" for h in exact(layer, sentence + " zzqx", fastlayer.Filters(), 10, 1))


def test_case_then_zone_rank_literal_papers(layer):
    hits = exact(layer, "assembly index", fastlayer.Filters(), 10, 1)
    assert [h["id"] for h in hits[:2]] == ["doi:10.1/at", "doi:10.1/old"]
    assert hits[1]["passages"][0]["field"] == "title"


def test_keywords_follow_literals(layer):
    hits = exact(layer, "RNA guided cutting of DNA", fastlayer.Filters(), 10, 1)
    assert hits and all(h["match"] == "keyword" for h in hits)
    assert hits[0]["id"] == "doi:10.1/cas"


def test_filters_papers_cannot_meet_answer_nothing(layer):
    for args in ({"kind": "journal"}, {"project": "awm"}, {"scope": "dev"}):
        assert exact(layer, "assembly", fastlayer.Filters.from_args(args), 10, 1) == []
    assert exact(layer, "assembly", fastlayer.Filters.from_args({"kind": "journal,paper"}), 10, 1)
    recent = exact(layer, "assembly index", fastlayer.Filters.from_args({"after": "2000-01-01"}), 10, 1)
    assert [h["id"] for h in recent] == ["doi:10.1/at"]


def test_cached_stems_agree_with_fresh_ones(monkeypatch):
    words = ["assembly", "indices", "molecules", "___", "running", "co2"]
    monkeypatch.setattr(fastlayer, "_stem_cache", {})
    fresh = fastlayer.stems(words)
    assert fastlayer.stems(words) == fresh
    assert fastlayer.stems(["running", "new_word", "indices"]) == {
        "running": fresh["running"], "indices": fresh["indices"], **fastlayer.stems(["new_word"])}
    assert "___" not in fresh and fresh["indices"] == "indic"


def test_a_literal_with_like_wildcards_matches_only_itself(tmp_path):
    _base(tmp_path / "papers.db", {
        "a": ("", "Clustering at 100% identity with p_value cut", 2020, "", ""),
        "b": ("", "Clustering at 100 x identity with pXvalue cut", 2020, "", "")})
    layer = fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "delta.db")
    for needle in ("100% identity", "p_value"):
        assert [h["id"] for h in exact(layer, needle, fastlayer.Filters(), 10, 1)
                if h["match"] == "literal"] == ["a"], needle


def test_a_base_of_another_schema_is_not_read(tmp_path):
    _base(tmp_path / "papers.db", {"p": ("", "Assembly theory", 2023, "", "")}, schema=99)
    layer = fastlayer.FastLayer(tmp_path / "papers.db", tmp_path / "delta.db")
    assert exact(layer, "Assembly", fastlayer.Filters(), 10, 1) == []


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
    hits = exact(layer, "iron oxidisers", fastlayer.Filters(), 10, 1)
    assert [h["id"] for h in hits] == ["zotero:users/0/CCC"]
    assert layer.status()["delta"]["library"] == 3

    _base(tmp_path / "papers.db", {"doi:10.1/vent": ("", "Microbial mats of the hydrothermal vent field", 2024,
                                                      "Vent mats host iron oxidisers.", "")})
    assert layer.remap() == {"remapped": 1}
    assert [h["id"] for h in exact(layer, "iron oxidisers", fastlayer.Filters(), 10, 1)] == ["doi:10.1/vent"]


async def test_the_query_verb_answers_in_sections(layer, monkeypatch):
    monkeypatch.setattr(hub_adapter, "PAPERS", layer)
    out = await hub_adapter.HANDLERS["query"]({"query": "shortest construction path", "limit": 3})
    assert set(out) == {"exact", "related", "graph"}
    assert out["exact"][0]["id"] == "doi:10.1/at"


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
