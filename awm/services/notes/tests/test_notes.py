"""Notes service unit tests.

Exercise the CRUD + trash lifecycle + keyword/fuzzy search against a temp DB and
a temp file store. The embedding model (sentence-transformers) is heavy and not
needed to test this logic, so ``index``'s embed/semantic helpers are stubbed —
semantic ranking is the shared ``awm.persistence.embeddings`` stack the writing
service already covers.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from awm.notes import config, db, index, notes, rooms, store
from awm.persistence.embeddings import EMBEDDINGS_DDL


@pytest.fixture(autouse=True)
def _clean_rooms():
    """The room registry is a process-global; wipe it between tests so a note
    opened in one test can't leak its live content into another."""
    rooms._ROOMS.clear()
    yield
    rooms._ROOMS.clear()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # Files land under a temp dir instead of AWM_DIR/services/notes/files.
    files = tmp_path / "files"
    files.mkdir()
    monkeypatch.setattr(config, "files_dir", lambda: files)

    # Stub the embedding side so tests never load the model.
    monkeypatch.setattr(index, "embed_note", lambda *a, **k: None)
    monkeypatch.setattr(index, "drop_embedding", lambda *a, **k: None)
    monkeypatch.setattr(index, "search_semantic", lambda *a, **k: [])

    c = sqlite3.connect(tmp_path / "notes.db")
    c.row_factory = sqlite3.Row
    c.executescript(db.NOTES_DDL + EMBEDDINGS_DDL)
    yield c
    c.close()


def test_create_writes_file_and_row(conn):
    n = notes.create(conn, path="research/avarice/log", content="# hello\nworld")
    assert n["path"] == "research/avarice/log"
    assert n["content"] == "# hello\nworld"
    # The on-disk file exists and is uuid-named.
    assert store.file_path(n["id"]).exists()
    assert store.read(n["id"]) == "# hello\nworld"
    assert n["file_path"].endswith(f"{n['id']}.md")


def test_save_updates_content_and_path(conn):
    n = notes.create(conn, path="a", content="one")
    notes.save(conn, n["id"], content="one two three", path="a/b")
    got = notes.get(conn, n["id"])
    assert got["path"] == "a/b"
    assert got["content"] == "one two three"
    assert store.read(n["id"]) == "one two three"


def test_save_path_only_keeps_content(conn):
    n = notes.create(conn, path="a", content="keepme")
    notes.save(conn, n["id"], path="renamed")
    got = notes.get(conn, n["id"])
    assert got["path"] == "renamed"
    assert got["content"] == "keepme"


def test_tree_splits_active_and_trashed(conn):
    a = notes.create(conn, path="one", content="x")
    b = notes.create(conn, path="two", content="y")
    notes.trash(conn, b["id"])
    tree = notes.tree(conn, include_trashed=True)
    active_ids = {r["id"] for r in tree["active"]}
    trashed_ids = {r["id"] for r in tree["trashed"]}
    assert a["id"] in active_ids and b["id"] not in active_ids
    assert b["id"] in trashed_ids


def test_trash_restore_roundtrip(conn):
    n = notes.create(conn, path="p", content="c")
    notes.trash(conn, n["id"])
    assert notes.get(conn, n["id"])["deleted_at"] is not None
    notes.restore(conn, n["id"])
    assert notes.get(conn, n["id"])["deleted_at"] is None


def test_purge_removes_file_and_row(conn):
    n = notes.create(conn, path="p", content="c")
    path = store.file_path(n["id"])
    notes.purge(conn, n["id"])
    assert not path.exists()
    with pytest.raises(ValueError):
        notes.get(conn, n["id"])


def test_purge_expired_only_old_trash(conn):
    fresh = notes.create(conn, path="fresh", content="c")
    old = notes.create(conn, path="old", content="c")
    notes.trash(conn, fresh["id"])
    notes.trash(conn, old["id"])
    # Backdate `old`'s deletion beyond the TTL.
    stale = (datetime.now(timezone.utc) - timedelta(days=config.TRASH_TTL_DAYS + 1)).isoformat()
    conn.execute("UPDATE notes SET deleted_at=? WHERE id=?", (stale, old["id"]))
    conn.commit()
    res = notes.purge_expired(conn)
    assert old["id"] in res["purged"]
    assert fresh["id"] not in res["purged"]


def test_keyword_search(conn):
    notes.create(conn, path="proj/alpha", content="the mitochondria is the powerhouse")
    notes.create(conn, path="proj/beta", content="unrelated content about ledgers")
    res = notes.search(conn, query="mitochondria")
    assert res["count"] == 1
    assert res["results"][0]["path"] == "proj/alpha"
    assert "snippet" in res["results"][0]


def test_keyword_search_tolerates_punctuation(conn):
    notes.create(conn, path="p", content="hello world")
    # Punctuation around real words must not raise an FTS5 syntax error, and the
    # note is still found (keyword search AND-s the extracted word tokens).
    res = notes.search(conn, query='"hello", world!')
    assert res["count"] == 1
    # A query that is only punctuation/operators must not raise either.
    assert notes.search(conn, query="()/ AND *")["count"] == 0


def test_fuzzy_search_typo_tolerant(conn):
    notes.create(conn, path="research/avarice/notes", content="quant models")
    # A typo'd title still matches via fuzzy.
    res = notes.search(conn, fuzzy="avarice")
    assert any("avarice" in r["path"] for r in res["results"])
    res2 = notes.search(conn, fuzzy="avarce")  # missing an 'i'
    assert any("avarice" in r["path"] for r in res2["results"])


def test_search_excludes_trashed_by_default(conn):
    n = notes.create(conn, path="p", content="findme keyword")
    notes.trash(conn, n["id"])
    assert notes.search(conn, query="findme")["count"] == 0
    assert notes.search(conn, query="findme", include_trashed=True)["count"] == 1


def test_empty_search_lists_recent(conn):
    notes.create(conn, path="a", content="1")
    notes.create(conn, path="b", content="2")
    res = notes.search(conn)
    assert res["count"] == 2


# ---------------------------------------------------------------------------
# Live collaboration rooms + lazy flush
# ---------------------------------------------------------------------------


def test_collab_open_snapshots_disk(conn):
    n = notes.create(conn, path="p", content="seed body")
    snap = notes.collab_open(conn, n["id"])
    assert snap == {"version": 0, "content": "seed body"}


def test_collab_edit_fast_path(conn):
    n = notes.create(conn, path="p", content="hello")
    res = notes.collab_edit(n["id"], 0, "hello world")
    assert res["changed"] is True and res["version"] == 1
    assert res["content"] == "hello world"
    # A no-op edit against the current version doesn't bump.
    same = notes.collab_edit(n["id"], 1, "hello world")
    assert same["changed"] is False and same["version"] == 1


def test_collab_edit_three_way_merge(conn):
    """Two clients editing from the same base both survive (Google-Docs-lite)."""
    n = notes.create(conn, path="p", content="the quick brown fox")
    # Client A inserts at the front (base 0).
    notes.collab_edit(n["id"], 0, "the very quick brown fox")
    # Client B, still on base 0, appends at the end.
    res = notes.collab_edit(n["id"], 0, "the quick brown fox jumps")
    assert res["content"] == "the very quick brown fox jumps"
    assert res["version"] == 2


def test_get_prefers_live_room_over_disk(conn):
    n = notes.create(conn, path="p", content="on disk")
    notes.collab_edit(n["id"], 0, "in memory, not yet flushed")
    # Disk is still the created content until a flush…
    assert store.read(n["id"]) == "on disk"
    # …but readers (agent/CLI/other tab) see the live room content.
    assert notes.get(conn, n["id"])["content"] == "in memory, not yet flushed"


def test_flush_persists_and_reindexes(conn):
    n = notes.create(conn, path="p", content="original")
    notes.collab_edit(n["id"], 0, "flushed keyword content")
    flushed = rooms.flush_all(conn)
    assert n["id"] in flushed
    # File + FTS now reflect the in-memory edit.
    assert store.read(n["id"]) == "flushed keyword content"
    assert notes.search(conn, query="keyword")["count"] == 1
    # A second flush with nothing dirty is a no-op.
    assert rooms.flush_all(conn) == []


def test_flush_clears_dirty_and_survives_reopen(conn):
    n = notes.create(conn, path="p", content="a")
    notes.collab_edit(n["id"], 0, "a b c")
    rooms.flush_all(conn)
    room = rooms._ROOMS[n["id"]]
    assert room.dirty is False
    assert room.content == "a b c"


def test_save_reconciles_open_room(conn):
    n = notes.create(conn, path="p", content="one")
    rooms.open_room(n["id"])
    notes.save(conn, n["id"], content="two")
    # The out-of-band CLI/agent write is reflected in the live room too.
    assert rooms.live_content(n["id"]) == "two"


def test_trash_drops_room(conn):
    n = notes.create(conn, path="p", content="x")
    rooms.open_room(n["id"])
    notes.trash(conn, n["id"])
    assert rooms.live_content(n["id"]) is None


def test_vocab_crud(conn):
    assert notes.vocab_list(conn)["terms"] == []
    notes.vocab_add(conn, "scadc")
    notes.vocab_add(conn, "avarice")
    notes.vocab_add(conn, "scadc")  # dupe ignored
    assert set(notes.vocab_list(conn)["terms"]) == {"scadc", "avarice"}
    notes.vocab_remove(conn, "scadc")
    assert notes.vocab_list(conn)["terms"] == ["avarice"]
