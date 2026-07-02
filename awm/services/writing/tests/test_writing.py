"""Tests for awm.writing — corpus search + curation on an isolated service DB.

The embedding-backed paths (add/embed/semantic) require the workspace embedding
stack (sentence-transformers + sqlite-vec); those tests skip if it's absent.
Keyword/metadata paths, the manifest surface split, and tag validation run
without any model.
"""

from __future__ import annotations

import importlib.util

import pytest

pytestmark = [pytest.mark.writing]

_HAS_EMBED = (
    importlib.util.find_spec("sentence_transformers") is not None
    and importlib.util.find_spec("sqlite_vec") is not None
)
needs_embed = pytest.mark.skipif(not _HAS_EMBED, reason="embedding stack not installed")


# ---------------------------------------------------------------------------
# Fixtures — isolated service DB in a temp dir (mirrors the artifacts pattern)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_writing_db(tmp_path, monkeypatch):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    monkeypatch.setenv("AWM_WORKSPACE", str(tmp_path))
    import awm.persistence.databases as dbmod
    monkeypatch.setattr(dbmod, "SERVICES_DIR", services_dir, raising=False)
    import awm.writing.dao as daomod
    monkeypatch.setattr(daomod, "_initialized", False)
    daomod.init()
    yield tmp_path


@pytest.fixture
def conn():
    from awm.writing import dao
    c = dao.connect()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Manifest surface split (no DB / model needed)
# ---------------------------------------------------------------------------


def test_manifest_surface_split():
    from awm.writing.hub_adapter import API_MANIFEST

    fns = {f["name"]: f for f in API_MANIFEST["functions"]}
    reads = {"search", "get", "stats", "vocab", "feed"}
    writes = {"add", "retag", "remove", "link_dup", "unlink_dup",
              "import", "embed", "dedup"}
    assert reads | writes == set(fns)
    # read verbs omit surfaces (default all three); write verbs are cli/http only
    for name in reads:
        assert "surfaces" not in fns[name]
    for name in writes:
        assert fns[name]["surfaces"] == ["cli", "http"]
    # every handler is wired
    from awm.writing.hub_adapter import HANDLERS
    assert set(HANDLERS) == set(fns)
    # long-runners declare a timeout
    for name in ("import", "embed", "dedup"):
        assert fns[name].get("timeout")


def test_vocab():
    from awm.writing import corpus
    v = corpus.vocab()
    assert "essay" in v["vocab"]["form"]
    assert "grade" in v["single_value_facets"]


def test_add_rejects_bad_tag(conn):
    from awm.writing import corpus
    with pytest.raises(ValueError):
        conn2 = conn
        corpus.add(conn2, text="hi there", name="x", tag=["form:not-a-real-form"])


# ---------------------------------------------------------------------------
# Round trips (need the embedding stack)
# ---------------------------------------------------------------------------


@needs_embed
def test_add_search_get_retag_remove(conn):
    from awm.writing import corpus

    a = corpus.add(conn, text="The trolley problem asks whether one may divert a "
                              "runaway trolley to kill one instead of five.",
                   name="trolley", type="academic",
                   tag=["form:essay", "grade:style-grade"])
    b = corpus.add(conn, text="A sonnet about the sea and longing, in iambic lines.",
                   name="sonnet", type="creative-personal",
                   tag=["form:creative", "grade:acceptable"])
    assert a["id"] != b["id"]

    # keyword search (FTS5)
    hits = corpus.search(conn, query="trolley")
    assert any(h["id"] == a["id"] for h in hits["results"])

    # metadata + tag filter
    filtered = corpus.search(conn, type="academic", tag=["grade:style-grade"])
    assert [h["id"] for h in filtered["results"]] == [a["id"]]

    # get with text
    got = corpus.get(conn, a["id"])
    assert "trolley" in got["text"] and got["grade"] == "style-grade"

    # retag
    corpus.retag(conn, a["id"], add=["register:argumentative"], rm=[])
    assert "register:argumentative" in corpus.get(conn, a["id"])["tags"]

    # stats
    st = corpus.stats(conn)
    assert st["samples"] == 2 and st["embedded"] == 2

    # remove drops row + embedding
    corpus.remove(conn, a["id"])
    assert corpus.stats(conn)["samples"] == 1
    assert corpus.stats(conn)["embedded"] == 1


@needs_embed
def test_semantic_search_and_feed(conn):
    from awm.writing import corpus

    corpus.add(conn, text="An argument about whether intelligent machines deserve "
                          "moral rights and legal personhood.",
               name="ai-rights", type="academic",
               tag=["form:essay", "grade:style-grade"])
    corpus.add(conn, text="A recipe for spaghetti with a rich tomato sauce.",
               name="recipe", type="unsorted", tag=["grade:weak"])

    res = corpus.search(conn, semantic="do robots have rights?", k=1)
    assert res["results"] and res["results"][0]["name"] == "ai-rights"
    assert res["results"][0]["score"] is not None

    # feed defaults to the style-grade keepers, with full text
    feed = corpus.feed(conn)
    assert feed["count"] == 1
    assert feed["records"][0]["name"] == "ai-rights"
    assert "machines" in feed["records"][0]["text"]


@needs_embed
def test_link_unlink_dup_excluded_from_search(conn):
    from awm.writing import corpus

    a = corpus.add(conn, text="Version one of an essay about civic duty and voting.",
                   name="civic-v1", type="academic", tag=["grade:style-grade"])
    b = corpus.add(conn, text="Version two of an essay about civic duty and voting, "
                             "slightly expanded with another paragraph.",
                   name="civic-v2", type="academic", tag=["grade:style-grade"])

    corpus.link_dup(conn, a["id"], [b["id"]])
    # duplicate excluded by default
    names = {h["id"] for h in corpus.search(conn, query="civic")["results"]}
    assert b["id"] not in names and a["id"] in names
    # included when asked
    with_dups = {h["id"] for h in corpus.search(conn, query="civic", include_dups=True)["results"]}
    assert b["id"] in with_dups

    corpus.unlink_dup(conn, b["id"])
    names2 = {h["id"] for h in corpus.search(conn, query="civic")["results"]}
    assert b["id"] in names2
