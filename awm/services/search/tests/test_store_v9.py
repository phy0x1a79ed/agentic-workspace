"""Schema 9: the documents move to LanceDB; the registry, judgements and usage log stay in SQLite."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone

import pytest

from awm.persistence.databases import init_db_at
from awm.search import store

#: The v8 tables as released, cut to what the move touches.
V8 = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (8);
CREATE TABLE sources (name TEXT PRIMARY KEY, service TEXT NOT NULL, mode TEXT NOT NULL, spec TEXT NOT NULL,
    present INTEGER NOT NULL DEFAULT 1, synced_at REAL, attempt_at REAL, last_error TEXT,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL);
CREATE TABLE documents (rid INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
    doc_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '');
CREATE VIRTUAL TABLE documents_exact USING fts5(title, text, content='documents', content_rowid='rid',
    tokenize='trigram');
CREATE TRIGGER documents_exact_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_exact(rowid, title, text) VALUES (new.rid, new.title, new.text);
END;
CREATE TABLE passages (id INTEGER PRIMARY KEY, key TEXT NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE passages_words USING fts5(text, content='passages', content_rowid='id');
CREATE TRIGGER passages_words_ai AFTER INSERT ON passages BEGIN
    INSERT INTO passages_words(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE doc_values (rid INTEGER NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL);
CREATE TRIGGER doc_values_ad AFTER DELETE ON documents BEGIN DELETE FROM doc_values WHERE rid = old.rid; END;
CREATE TABLE embeddings (id INTEGER PRIMARY KEY, source_type TEXT, source_id TEXT, chunk_text TEXT);
CREATE VIRTUAL TABLE embeddings_fts USING fts5(text);
""" + store._JUDGEMENTS + store._USAGE + store._USAGE_SCORE


def test_the_move_drops_the_documents_and_keeps_the_feedback(tmp_path):
    path = tmp_path / "search.db"
    conn = sqlite3.connect(path)
    conn.executescript(V8)
    conn.execute("INSERT INTO sources (name, service, mode, spec, synced_at, first_seen, last_seen)"
                 " VALUES ('journals', 'journal', 'index', '{}', 5, 1, 1)")
    conn.execute("INSERT INTO documents (key, source, doc_id, title) VALUES ('journals:j1', 'journals', 'j1', 't')")
    conn.execute("INSERT INTO passages (key, text) VALUES ('journals:j1', 't')")
    conn.execute("INSERT INTO judgements (query, source, doc_id, verdict, ts) VALUES ('q', 'journals', 'j1',"
                 " 'relevant', 1)")
    conn.execute("INSERT INTO usage (ts, source, doc_id) VALUES (1, 'journals', 'j1')")
    conn.commit()
    conn.close()
    init_db_at(path, store.SCHEMA_SQL, schema_version=store.SCHEMA_VERSION, migrations=store.MIGRATIONS)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert not names & {"documents", "documents_exact", "passages", "passages_words", "doc_values",
                        "embeddings", "embeddings_fts", "documents_exact_ai", "doc_values_ad"}
    row = conn.execute("SELECT * FROM sources").fetchone()
    assert (row["cursor"], row["epoch"], row["layout"], row["synced_at"]) == (None, None, None, None)
    assert conn.execute("SELECT COUNT(*) FROM judgements").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9


def test_a_fresh_database_and_a_moved_one_have_the_same_tables(tmp_path):
    moved, fresh = tmp_path / "moved.db", tmp_path / "fresh.db"
    conn = sqlite3.connect(moved)
    conn.executescript(V8)
    conn.close()
    for path in (moved, fresh):
        init_db_at(path, store.SCHEMA_SQL, schema_version=store.SCHEMA_VERSION, migrations=store.MIGRATIONS)

    def shape(path):
        conn = sqlite3.connect(path)
        tables = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        return {t: sorted(r[1] for r in conn.execute(f"PRAGMA table_info({t})")) for t in tables}

    assert shape(moved) == shape(fresh)


def test_feed_state_round_trips(search_db):
    conn = store.connect()
    store.record_registry(conn, {"journals": {"service": "journal", "mode": "index"}}, {"journals"})
    assert store.feed_state(conn, "journals") == {"cursor": None, "epoch": None, "layout": None}
    store.save_feed_state(conn, "journals", "3:j2", "e1", "1.abc.stub")
    assert store.feed_state(conn, "journals") == {"cursor": "3:j2", "epoch": "e1", "layout": "1.abc.stub"}
    assert store.feed_state(conn, "nothing") == {"cursor": None, "epoch": None, "layout": None}


DAY = 86400.0


def score(conn, source="journals", doc_id="a", node=""):
    row = conn.execute("SELECT s FROM usage_score WHERE source=? AND doc_id=? AND node=?",
                       (source, doc_id, node)).fetchone()
    return row[0] if row else None


def test_logaddexp_is_stable():
    assert store.logaddexp(0.0, 0.0) == pytest.approx(math.log(2))
    assert store.logaddexp(1e4, -1e4) == 1e4
    assert store.logaddexp(-1e4, -1e4) == pytest.approx(-1e4 + math.log(2))


def test_first_get_sets_lambda_t_and_two_gets_add_ln2(search_db):
    conn = store.connect()
    t = store.EPOCH + 10 * DAY
    store.log_use(conn, {"source": "journals", "doc_id": "a", "node": "n", "ts": t})
    x = store.LAMBDA * 10
    assert score(conn, node="n") == pytest.approx(x)
    store.log_use(conn, {"source": "journals", "doc_id": "a", "node": "n", "ts": t})
    assert score(conn, node="n") == pytest.approx(x + math.log(2))
    assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 2
    assert score(conn, node="other") is None


def test_a_get_thirty_days_later_outweighs_by_two(search_db):
    conn = store.connect()
    store.log_use(conn, {"source": "journals", "doc_id": "a", "ts": store.EPOCH})
    store.log_use(conn, {"source": "journals", "doc_id": "b", "ts": store.EPOCH + 30 * DAY})
    a, b = score(conn, doc_id="a"), score(conn, doc_id="b")
    assert a == pytest.approx(0.0) and b == pytest.approx(math.log(2))
    store.log_use(conn, {"source": "journals", "doc_id": "a", "ts": store.EPOCH + 30 * DAY})
    assert score(conn, doc_id="a") == pytest.approx(math.log(1 + 2))


def test_epoch_is_2026_01_01_utc():
    assert store.EPOCH == datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
