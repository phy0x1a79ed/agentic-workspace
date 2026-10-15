"""The search service's database: the source registry, the documents, their passages, the corpus.

Every archive document is one row in ``documents`` keyed ``source:id``. Two FTS5
indexes serve the exact section. ``documents_exact`` holds trigrams of whole
documents and finds any literal of three characters or more, even one that
crosses a passage boundary. ``passages_words`` holds porter-stemmed words of
passages and ranks keyword matches by BM25. Both are external-content tables
kept in step by triggers on an explicit integer key, which no VACUUM renumbers.

Each document is also one entry in the retrieval engine under the single source
type :data:`CORPUS`, which serves the related section. One corpus matters
because the engine normalises scores per call.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from awm.persistence import embeddings as engine
from awm.persistence.databases import get_connection, init_service_db

from awm.search import passages as cutter

SERVICE = "search"
SCHEMA_VERSION = 2
CORPUS = "doc"

_SOURCES = """\
CREATE TABLE IF NOT EXISTS sources (
    name        TEXT PRIMARY KEY,
    service     TEXT NOT NULL,
    mode        TEXT NOT NULL,
    spec        TEXT NOT NULL,       -- the Source as JSON, kept so a frozen source stays described
    present     INTEGER NOT NULL DEFAULT 1,
    synced_at   REAL,                -- end of the last full pass that succeeded
    attempt_at  REAL,
    last_error  TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
"""

_DOCUMENTS = ["""CREATE TABLE IF NOT EXISTS documents (
    rid      INTEGER PRIMARY KEY,
    key      TEXT NOT NULL UNIQUE,   -- source:id
    source   TEXT NOT NULL,
    doc_id   TEXT NOT NULL,
    title    TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL DEFAULT '',
    project  TEXT NOT NULL DEFAULT '',
    scope    TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL DEFAULT '',
    ts       TEXT NOT NULL DEFAULT '', -- ISO 8601 UTC, second resolution, so text order is time order
    url      TEXT NOT NULL DEFAULT ''
);""",
    "CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source, doc_id);",
    "CREATE INDEX IF NOT EXISTS idx_documents_filter ON documents(source, project, scope, kind, ts);"]

_TEXT_INDEXES = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_exact USING fts5(
    title, text, content='documents', content_rowid='rid', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS documents_exact_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_exact(rowid, title, text) VALUES (new.rid, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_exact_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_exact(documents_exact, rowid, title, text)
        VALUES ('delete', old.rid, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_exact_au AFTER UPDATE OF title, text ON documents BEGIN
    INSERT INTO documents_exact(documents_exact, rowid, title, text)
        VALUES ('delete', old.rid, old.title, old.text);
    INSERT INTO documents_exact(rowid, title, text) VALUES (new.rid, new.title, new.text);
END;

CREATE TABLE IF NOT EXISTS passages (
    id       INTEGER PRIMARY KEY,
    key      TEXT NOT NULL,          -- the document's source:id
    field    TEXT NOT NULL,          -- title or text: the field start and stop index into
    start    INTEGER NOT NULL,
    stop     INTEGER NOT NULL,
    section  TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passages_key ON passages(key, field, start);
CREATE VIRTUAL TABLE IF NOT EXISTS passages_words USING fts5(
    text, content='passages', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS passages_words_ai AFTER INSERT ON passages BEGIN
    INSERT INTO passages_words(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS passages_words_ad AFTER DELETE ON passages BEGIN
    INSERT INTO passages_words(passages_words, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""

SCHEMA_SQL = _SOURCES + "\n".join(_DOCUMENTS) + _TEXT_INDEXES + engine.EMBEDDINGS_DDL

MIGRATIONS = {(1, 2): "\n".join([
    "ALTER TABLE documents RENAME TO documents_v1;",
    "DROP INDEX IF EXISTS idx_documents_source;",
    "DROP INDEX IF EXISTS idx_documents_filter;",
    _DOCUMENTS[0],
    "INSERT INTO documents (key, source, doc_id, title, text, project, scope, kind, ts, url)"
    " SELECT key, source, doc_id, title, text, project, scope, kind, ts, url FROM documents_v1;",
    "DROP TABLE documents_v1;",
    *_DOCUMENTS[1:],
]) + _TEXT_INDEXES + "INSERT INTO documents_exact(documents_exact) VALUES ('rebuild');\n"}

_initialized = False


def init() -> None:
    """Idempotently create the search database and the engine's chunk store, and cut missing passages."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION, migrations=MIGRATIONS)
        _initialized = True
    conn = connect()
    try:
        engine.ensure_schema(conn)
        cut_missing(conn)
    finally:
        conn.close()


def connect() -> sqlite3.Connection:
    """A fresh connection to the search database (WAL, Row factory)."""
    return get_connection(SERVICE)


def normalize_ts(value: Any) -> str:
    """``2026-03-01T12:00:00Z`` for any ISO 8601 text; ``''`` when absent or unparseable.

    A naive value is read as UTC and a bare date as midnight. Text order on the
    result is time order, which is what the ``after`` and ``before`` filters rely on.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- sources ---------------------------------------------------------------


def record_registry(conn: sqlite3.Connection, sources: dict[str, dict], present: set[str],
                    now: float | None = None) -> list[str]:
    """Store what the registry shows now; return the names that are new, changed or back.

    ``sources`` maps a name to its ``{service, mode, ...}`` spec. A stored source
    the registry no longer shows is kept and marked absent, never deleted: its
    documents stay searchable as frozen.
    """
    now = time.time() if now is None else now
    stored = {r["name"]: r for r in conn.execute("SELECT name, spec, present FROM sources")}
    news = []
    for name, spec in sources.items():
        blob = json.dumps(spec, sort_keys=True)
        row = stored.get(name)
        if row is None:
            conn.execute(
                "INSERT INTO sources (name, service, mode, spec, present, first_seen, last_seen)"
                " VALUES (?,?,?,?,1,?,?)", (name, spec["service"], spec["mode"], blob, now, now))
            news.append(name)
            continue
        if row["spec"] != blob or not row["present"]:
            news.append(name)
        conn.execute("UPDATE sources SET service=?, mode=?, spec=?, present=1, last_seen=? WHERE name=?",
                     (spec["service"], spec["mode"], blob, now, name))
    for name in stored.keys() - sources.keys():
        conn.execute("UPDATE sources SET present=0 WHERE name=?", (name,))
    conn.commit()
    return news


def source_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every stored source with its document count, sorted by name."""
    counts = dict(conn.execute("SELECT source, COUNT(*) FROM documents GROUP BY source").fetchall())
    out = []
    for r in conn.execute("SELECT * FROM sources ORDER BY name"):
        out.append({**dict(r), "spec": json.loads(r["spec"]), "present": bool(r["present"]),
                    "count": counts.get(r["name"], 0)})
    return out


def mark_attempt(conn: sqlite3.Connection, name: str, error: str | None,
                 now: float | None = None) -> None:
    now = time.time() if now is None else now
    if error is None:
        conn.execute("UPDATE sources SET attempt_at=?, synced_at=?, last_error=NULL WHERE name=?",
                     (now, now, name))
    else:
        conn.execute("UPDATE sources SET attempt_at=?, last_error=? WHERE name=?",
                     (now, error[:300], name))
    conn.commit()


# -- documents -------------------------------------------------------------

_FIELDS = ("title", "text", "project", "scope", "kind", "ts", "url")


def key_of(source: str, doc_id: str) -> str:
    return f"{source}:{doc_id}"


def engine_document(row: dict[str, Any]) -> engine.Document:
    where = "/".join(x for x in (row["project"], row["scope"]) if x)
    context = " · ".join(x for x in (row["source"], where, row["kind"]) if x)
    return engine.Document(row["key"], row["text"], title=row["title"], context=context,
                           date=row["ts"][:10])


def clean(source: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    """A stored row from one exported document, or ``None`` when it has no id or no content."""
    doc_id = str(raw.get("id") or "")
    title, text = str(raw.get("title") or ""), str(raw.get("text") or "")
    if not doc_id or not (title.strip() or text.strip()):
        return None
    return {"key": key_of(source, doc_id), "source": source, "doc_id": doc_id, "title": title,
            "text": text, "project": str(raw.get("project") or ""), "scope": str(raw.get("scope") or ""),
            "kind": str(raw.get("kind") or ""), "ts": normalize_ts(raw.get("ts")),
            "url": str(raw.get("url") or "")}


def _cut(conn: sqlite3.Connection, key: str, title: str, text: str) -> None:
    conn.execute("DELETE FROM passages WHERE key=?", (key,))
    rows = [(key, "title", 0, len(title), "", title)] if title.strip() else []
    rows += [(key, "text", p.start, p.end, p.section, text[p.start:p.end]) for p in cutter.split(text)]
    conn.executemany("INSERT INTO passages (key, field, start, stop, section, text) VALUES (?,?,?,?,?,?)",
                     rows)


def put_documents(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Store ``rows``, writing only what changed; return how many changed.

    An unchanged document costs one read, so a full export pass over a quiet
    source rewrites no index.
    """
    rows = list(rows)
    held = {r["key"]: r for r in conn.execute(
        f"SELECT key, {', '.join(_FIELDS)} FROM documents WHERE key IN ({','.join('?' * len(rows))})",
        [r["key"] for r in rows])} if rows else {}
    changed = [r for r in rows if r["key"] not in held or any(held[r["key"]][f] != r[f] for f in _FIELDS)]
    conn.executemany(
        "INSERT INTO documents (key, source, doc_id, title, text, project, scope, kind, ts, url)"
        " VALUES (:key,:source,:doc_id,:title,:text,:project,:scope,:kind,:ts,:url)"
        " ON CONFLICT(key) DO UPDATE SET title=excluded.title, text=excluded.text,"
        " project=excluded.project, scope=excluded.scope, kind=excluded.kind, ts=excluded.ts,"
        " url=excluded.url", changed)
    for r in changed:
        old = held.get(r["key"])
        if old is None or (old["title"], old["text"]) != (r["title"], r["text"]):
            _cut(conn, r["key"], r["title"], r["text"])
    conn.commit()
    return len(changed)


def cut_missing(conn: sqlite3.Connection) -> int:
    """Cut passages for every stored document that has none, as after the move to schema 2."""
    rows = conn.execute("SELECT key, title, text FROM documents"
                        " WHERE key NOT IN (SELECT DISTINCT key FROM passages)").fetchall()
    for r in rows:
        _cut(conn, r["key"], r["title"], r["text"])
    conn.commit()
    return len(rows)


def document_ids(conn: sqlite3.Connection, source: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT doc_id FROM documents WHERE source=?", (source,))}


def drop_documents(conn: sqlite3.Connection, source: str, doc_ids: Iterable[str]) -> int:
    gone = list(doc_ids)
    for doc_id in gone:
        key = key_of(source, doc_id)
        engine.delete_document(conn, CORPUS, key)
        conn.execute("DELETE FROM passages WHERE key=?", (key,))
        conn.execute("DELETE FROM documents WHERE key=?", (key,))
    conn.commit()
    return len(gone)


def get_document(conn: sqlite3.Connection, source: str, doc_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM documents WHERE key=?", (key_of(source, doc_id),)).fetchone()
    return dict(row) if row else None
