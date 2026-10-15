"""Retrieval engine: chunking, headers, MaxP, filter-first search, fusion, staleness, upgrade."""

from __future__ import annotations

import sqlite3

import pytest

from awm.persistence import embeddings as E
from awm.persistence.search_testing import StubEmbedder


@pytest.fixture
def stub():
    emb = StubEmbedder()
    E.use_embedder(emb)
    yield emb
    E.use_embedder(None)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "svc.db")
    c.executescript(E.EMBEDDINGS_DDL)
    c.execute("CREATE TABLE items (id TEXT PRIMARY KEY, kind TEXT)")
    yield c
    c.close()


def _filler(n: int, topic: str = "alpha") -> str:
    return "\n\n".join(f"Paragraph {i} discusses {topic} routine maintenance and upkeep." for i in range(n))


# ---------------------------------------------------------------- chunking


def test_chunks_respect_budget_and_sections():
    body = "# Setup\n" + _filler(30) + "\n\n## Results\nThe needle sits here."
    chunks = E.chunk(body, 40, E._approx_tokens)
    assert all(n <= 40 for n in E._approx_tokens([t for _, t in chunks]))
    assert chunks[0][0] == "Setup"
    assert chunks[-1][1].endswith("The needle sits here.")


def test_long_block_splits_at_sentence_ends():
    body = " ".join(f"Sentence number {i} ends here." for i in range(40))
    chunks = E.chunk(body, 30, E._approx_tokens)
    assert len(chunks) > 1
    assert all(t.endswith("here.") for _, t in chunks)


def test_oversized_sentence_splits_at_words():
    body = " ".join(f"w{i}" for i in range(500))
    chunks = E.chunk(body, 50, E._approx_tokens)
    assert all(n <= 50 for n in E._approx_tokens([t for _, t in chunks]))
    assert " ".join(t for _, t in chunks).split() == body.split()


def test_blank_body_never_counts_an_empty_batch():
    def strict(texts):
        assert texts, "tokenizers reject an empty batch"
        return E._approx_tokens(texts)
    assert E.chunk("  \n\n \t", 40, strict) == []


def test_header_joins_present_parts():
    doc = E.Document("1", "b", title="Deploy notes", context="awm/dev", date="2026-09-26")
    assert E.header(doc, "Issues") == "Deploy notes — awm/dev · 2026-09-26 — Issues"
    assert E.header(E.Document("2", "b")) == ""


# ---------------------------------------------------------------- index + search


def test_needle_deep_in_a_long_document_is_found(conn, stub):
    E.index_document(conn, "post", E.Document(
        "long", _filler(40) + "\n\nWe replaced the flux capacitor gasket.", title="Maintenance"))
    for i in range(5):
        E.index_document(conn, "post", E.Document(f"other{i}", _filler(3, f"topic{i}")))
    hits = E.search(conn, "flux capacitor gasket", source_type="post").hits
    assert hits[0]["source_id"] == "long"
    assert "capacitor" in hits[0]["snippet"]
    assert conn.execute("SELECT COUNT(*) FROM embeddings WHERE source_id='long'").fetchone()[0] > 1


def test_filter_applies_before_ranking(conn, stub):
    for i in range(30):
        E.index_document(conn, "post", E.Document(f"m{i}", f"orbital launch window {i}"))
        conn.execute("INSERT INTO items VALUES (?, 'message')", (f"m{i}",))
    E.index_document(conn, "post", E.Document("j", "notes about something else, launch mentioned once"))
    conn.execute("INSERT INTO items VALUES ('j', 'journal')")
    unfiltered = [h["source_id"] for h in E.search(conn, "orbital launch window", source_type="post", limit=5).hits]
    assert "j" not in unfiltered
    hits = E.search(conn, "orbital launch window", source_type="post",
                    allowed="SELECT id FROM items WHERE kind = ?", params=("journal",)).hits
    assert [h["source_id"] for h in hits] == ["j"]


def test_source_types_are_separate(conn, stub):
    E.index_document(conn, "post", E.Document("1", "shared words here"))
    E.index_document(conn, "scope", E.Document("1", "shared words here"))
    E.delete_document(conn, "scope", "1")
    assert [h["source_id"] for h in E.search(conn, "shared words", source_type="post").hits] == ["1"]
    assert E.search(conn, "shared words", source_type="scope").hits == []


@pytest.mark.parametrize("q", ['a-b "c', "NEAR(x y)", "*", "col:val OR", "'); DROP TABLE x;--"])
def test_punctuation_is_inert(conn, stub, q):
    E.index_document(conn, "post", E.Document("1", "a-b c val x y"))
    E.search(conn, q, source_type="post")


def test_fuse_min_max_convex():
    fused = E.fuse({"a": 0.9, "b": 0.5}, {"b": 10.0, "c": 2.0}, alpha=0.4)
    assert fused["a"] == pytest.approx(0.4)
    assert fused["b"] == pytest.approx(0.6)
    assert fused["c"] == pytest.approx(0.0)


# ---------------------------------------------------------------- staleness + backfill


def test_unchanged_document_is_skipped(conn, stub):
    doc = E.Document("1", "some body text", title="T")
    assert E.index_document(conn, "post", doc) == "indexed"
    n = stub.encoded
    assert E.index_document(conn, "post", doc) == "unchanged"
    assert stub.encoded == n


def test_reindex_classifies_and_catches_up(conn, stub):
    docs = [E.Document(str(i), f"body {i}") for i in range(4)]
    first = E.reindex(conn, "post", docs)
    assert first["missing"] == 4 and first["indexed"] == 4
    docs[0] = E.Document("0", "a changed body")
    E.use_embedder(StubEmbedder("stub-b"))
    again = E.reindex(conn, "post", docs)
    assert (again["changed"], again["stale"], again["current"]) == (1, 3, 0)
    assert {r[0] for r in conn.execute("SELECT DISTINCT model FROM embeddings")} == {"stub-b"}
    assert E.reindex(conn, "post", docs)["current"] == 4


def test_reindex_prune_and_dry_run(conn, stub):
    E.reindex(conn, "post", [E.Document("keep", "x"), E.Document("gone", "y")])
    dry = E.reindex(conn, "post", [E.Document("keep", "x")], prune=True, dry_run=True)
    assert dry["pruned"] == 1
    assert conn.execute("SELECT COUNT(*) FROM embeddings WHERE source_id='gone'").fetchone()[0] == 1
    E.reindex(conn, "post", [E.Document("keep", "x")], prune=True)
    assert conn.execute("SELECT COUNT(*) FROM embeddings WHERE source_id='gone'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM embeddings_fts").fetchone()[0] == 1


def test_without_the_stack_keyword_search_still_answers(conn, monkeypatch):
    E.use_embedder(None)
    monkeypatch.setattr(E, "probe", lambda: {"available": False, "missing": ["sentence_transformers"]})
    assert E.index_document(conn, "post", E.Document("1", "quantum widget calibration")) == "keyword-only"
    res = E.search(conn, "widget calibration", source_type="post")
    assert [h["source_id"] for h in res.hits] == ["1"]
    assert res.degraded["fallback"] == "keyword"
    stub = StubEmbedder()
    E.use_embedder(stub)
    try:
        assert E.reindex(conn, "post", [E.Document("1", "quantum widget calibration")])["stale"] == 1
    finally:
        E.use_embedder(None)


# ---------------------------------------------------------------- upgrade


def test_one_row_table_upgrades_in_place(tmp_path, stub):
    c = sqlite3.connect(tmp_path / "old.db")
    c.execute("""CREATE TABLE embeddings (id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type TEXT NOT NULL, source_id TEXT NOT NULL, chunk_text TEXT NOT NULL,
        embedding BLOB NOT NULL, updated_at TEXT NOT NULL, UNIQUE(source_type, source_id))""")
    c.execute("INSERT INTO embeddings (source_type, source_id, chunk_text, embedding, updated_at)"
              " VALUES ('post', 'p1', 'legacy zeppelin text', ?, 't')", (b"\0" * 16,))
    c.commit()
    E.ensure_schema(c)
    E.ensure_schema(c)
    row = c.execute("SELECT chunk, model, content_hash FROM embeddings WHERE source_id='p1'").fetchone()
    assert row == (0, E._LEGACY_MODEL, None)
    assert [h["source_id"] for h in E.search(c, "zeppelin", source_type="post").hits] == ["p1"]
    assert E.reindex(c, "post", [E.Document("p1", "legacy zeppelin text")])["changed"] == 1
    c.close()
