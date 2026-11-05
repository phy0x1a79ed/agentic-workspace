"""Schema 8: doc_values, usage_score and the retirement of precedence and notes."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone

import pytest

from awm.persistence import embeddings as engine
from awm.persistence.databases import init_db_at
from awm.search import store


@pytest.fixture(autouse=True)
def stub_embedder():
    from awm.persistence.search_testing import StubEmbedder
    engine.use_embedder(StubEmbedder())
    yield
    engine.use_embedder(None)


@pytest.fixture()
def search_db(tmp_path, monkeypatch):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    import awm.persistence.databases as pdbs
    monkeypatch.setattr(pdbs, "SERVICES_DIR", services_dir)
    monkeypatch.setattr(store, "_initialized", False)
    store.init()
    return services_dir


def values(conn, key):
    rid = conn.execute("SELECT rid FROM documents WHERE key=?", (key,)).fetchone()[0]
    return sorted(tuple(r) for r in conn.execute("SELECT field, value FROM doc_values WHERE rid=?", (rid,)))


def row(i, **kw):
    return store.clean("journals", {"id": i, "title": f"t{i}", "text": f"body {i}", **kw})


def test_put_documents_fills_and_refreshes_values(search_db):
    conn = store.connect()
    store.put_documents(conn, [row("a", tags=["B", "a"], reply_to=["p1", "p2"])])
    assert values(conn, "journals:a") == [("replies_to", "p1"), ("replies_to", "p2"), ("tag", "a"), ("tag", "b")]
    store.put_documents(conn, [row("a", tags=["z"], reply_to=[])])
    assert values(conn, "journals:a") == [("tag", "z")]
    assert store.put_documents(conn, [row("a", tags=["z"])]) == 0
    assert values(conn, "journals:a") == [("tag", "z")]


def test_values_follow_the_document_out(search_db):
    conn = store.connect()
    store.put_documents(conn, [row("a", tags=["x"]), row("b", tags=["x"])])
    store.drop_documents(conn, "journals", ["a"])
    assert conn.execute("SELECT COUNT(*) FROM doc_values").fetchone()[0] == 1
    assert conn.execute("SELECT field, value FROM doc_values").fetchone()[:] == ("tag", "x")


def test_fresh_schema_has_the_v8_tables_and_index(search_db):
    conn = store.connect()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"doc_values", "usage_score", "idx_doc_values_lookup", "idx_documents_recent"} <= names
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 8 == store.SCHEMA_VERSION


def v7_database(path):
    """A database at schema 7: the current schema minus everything v8 added."""
    ddl = store.SCHEMA_SQL.replace(store._DOC_VALUES, "")
    init_db_at(path, ddl, schema_version=7)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_migration_backfills_and_retires(tmp_path):
    path = tmp_path / "search.db"
    conn = v7_database(path)
    engine.ensure_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "doc_values" not in names and "usage_score" not in names
    rows = [row("a", tags=["x", "y"], reply_to=["r1"]), row("b"),
            store.clean("precedence", {"id": "p", "title": "pt", "text": "pbody", "tags": ["x"]}),
            store.clean("notes", {"id": "n", "title": "nt", "text": "nbody"})]
    for r in rows:
        conn.execute("INSERT INTO documents (key, source, doc_id, title, text, project, scope, kind, ts, url, "
                     "tags, reply_to) VALUES (:key,:source,:doc_id,:title,:text,:project,:scope,:kind,:ts,"
                     ":url,:tags,:reply_to)", r)
        store._cut(conn, conn.execute("SELECT * FROM documents WHERE key=?", (r["key"],)).fetchone())
    for key in ("precedence:p", "notes:n", "journals:a"):
        engine.index_document(conn, store.CORPUS, engine.Document(key, "some text here", title="t"))
    now = 1.0
    for name in ("precedence", "notes", "journals"):
        conn.execute("INSERT INTO sources (name, service, mode, spec, first_seen, last_seen)"
                     " VALUES (?, 's', 'index', '{}', ?, ?)", (name, now, now))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM embeddings WHERE source_id='notes:n'").fetchone()[0] > 0
    conn.close()

    init_db_at(path, store.SCHEMA_SQL, schema_version=store.SCHEMA_VERSION, migrations=store.MIGRATIONS)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 8
    assert sorted(r[0] for r in conn.execute("SELECT key FROM documents")) == ["journals:a", "journals:b"]
    got = sorted(conn.execute("SELECT field, value FROM doc_values").fetchall())
    assert got == [("replies_to", "r1"), ("tag", "x"), ("tag", "y")]
    assert conn.execute("SELECT COUNT(*) FROM passages WHERE key IN ('precedence:p','notes:n')").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM passages WHERE key='journals:a'").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM embeddings WHERE source_id IN ('precedence:p','notes:n')"
                        ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM embeddings WHERE source_id='journals:a'").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM embeddings_fts").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert [r[0] for r in conn.execute("SELECT name FROM sources")] == ["journals"]
    assert conn.execute("SELECT COUNT(*) FROM documents_exact WHERE documents_exact MATCH '\"pbody\"'"
                        ).fetchone()[0] == 0


def test_migration_runs_again_without_duplicating(tmp_path):
    path = tmp_path / "search.db"
    conn = v7_database(path)
    r = row("a", tags=["x"])
    conn.execute("INSERT INTO documents (key, source, doc_id, title, text, tags, reply_to)"
                 " VALUES (?,?,?,?,?,?,?)", (r["key"], "journals", "a", "t", "b", r["tags"], ""))
    conn.commit()
    conn.close()
    for _ in range(2):
        conn = sqlite3.connect(path)
        conn.executescript(store.MIGRATIONS[(7, 8)])
        conn.close()
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT field, value FROM doc_values").fetchall() == [("tag", "x")]


def score(conn, source="journals", doc_id="a", node=""):
    r = conn.execute("SELECT s FROM usage_score WHERE source=? AND doc_id=? AND node=?",
                     (source, doc_id, node)).fetchone()
    return r[0] if r else None


DAY = 86400.0


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
    assert math.exp(b - a) == pytest.approx(2.0)
    store.log_use(conn, {"source": "journals", "doc_id": "a", "ts": store.EPOCH + 30 * DAY})
    assert score(conn, doc_id="a") == pytest.approx(math.log(1 + 2))


def test_epoch_is_2026_01_01_utc():
    assert store.EPOCH == datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
