"""The search service's database: the source registry, the document table, the corpus.

Every archive document is one row in ``documents`` keyed ``source:id``, and one
document in the retrieval engine under the single source type :data:`CORPUS`.
One corpus matters because the engine normalises scores per call: ranking each
source in its own corpus would make their scores incomparable.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from awm.persistence import embeddings as engine
from awm.persistence.databases import get_connection, init_service_db

SERVICE = "search"
SCHEMA_VERSION = 1
CORPUS = "doc"

SCHEMA_SQL = """\
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

CREATE TABLE IF NOT EXISTS documents (
    key      TEXT PRIMARY KEY,       -- source:id
    source   TEXT NOT NULL,
    doc_id   TEXT NOT NULL,
    title    TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL DEFAULT '',
    project  TEXT NOT NULL DEFAULT '',
    scope    TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL DEFAULT '',
    ts       TEXT NOT NULL DEFAULT '', -- ISO 8601 UTC, second resolution, so text order is time order
    url      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source, doc_id);
CREATE INDEX IF NOT EXISTS idx_documents_filter ON documents(source, project, scope, kind, ts);
""" + engine.EMBEDDINGS_DDL

_initialized = False


def init() -> None:
    """Idempotently create the search database and the engine's chunk store."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True
    conn = connect()
    try:
        engine.ensure_schema(conn)
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
    documents stay searchable as frozen. An unchanged registry writes nothing,
    because a query calls this and must not wait on a sweep's write lock.
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
        elif row["spec"] != blob or not row["present"]:
            news.append(name)
            conn.execute("UPDATE sources SET service=?, mode=?, spec=?, present=1, last_seen=? WHERE name=?",
                         (spec["service"], spec["mode"], blob, now, name))
    for name in stored.keys() - sources.keys():
        if stored[name]["present"]:
            conn.execute("UPDATE sources SET present=0 WHERE name=?", (name,))
    if conn.in_transaction:
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


def put_documents(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> None:
    conn.executemany(
        "INSERT INTO documents (key, source, doc_id, title, text, project, scope, kind, ts, url)"
        " VALUES (:key,:source,:doc_id,:title,:text,:project,:scope,:kind,:ts,:url)"
        " ON CONFLICT(key) DO UPDATE SET title=excluded.title, text=excluded.text,"
        " project=excluded.project, scope=excluded.scope, kind=excluded.kind, ts=excluded.ts,"
        " url=excluded.url", list(rows))
    conn.commit()


def document_ids(conn: sqlite3.Connection, source: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT doc_id FROM documents WHERE source=?", (source,))}


def drop_documents(conn: sqlite3.Connection, source: str, doc_ids: Iterable[str]) -> int:
    gone = list(doc_ids)
    for doc_id in gone:
        key = key_of(source, doc_id)
        engine.delete_document(conn, CORPUS, key)
        conn.execute("DELETE FROM documents WHERE key=?", (key,))
    conn.commit()
    return len(gone)


def get_document(conn: sqlite3.Connection, source: str, doc_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM documents WHERE key=?", (key_of(source, doc_id),)).fetchone()
    return dict(row) if row else None
