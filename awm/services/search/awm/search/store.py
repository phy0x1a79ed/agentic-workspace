"""The search service's own database: the source registry with each source's feed cursor, the judgements and
the usage log. Each index source's entries live in LanceDB (:mod:`awm.search.lance_store`).

A source's ``layout`` names the engine version, field declarations and model its tables were built with. A
stored layout that differs from the one the registry now asks for drops the tables, and the source reads its
feed again from the start.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from awm.persistence.databases import get_connection, init_service_db

SERVICE = "search"
SCHEMA_VERSION = 9

_SOURCES = """\
CREATE TABLE IF NOT EXISTS sources (
    name        TEXT PRIMARY KEY,
    service     TEXT NOT NULL,
    mode        TEXT NOT NULL,
    spec        TEXT NOT NULL,       -- the Source as JSON, kept so a frozen source stays described
    present     INTEGER NOT NULL DEFAULT 1,
    synced_at   REAL,                -- end of the last feed read that succeeded
    attempt_at  REAL,
    last_error  TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    cursor      TEXT,                -- the feed's `next` after the last page applied
    epoch       TEXT,                -- the feed's epoch that cursor belongs to
    layout      TEXT                 -- the layout the source's tables were built with
);
"""

VERDICTS = ("relevant", "irrelevant")
SECTIONS = ("exact", "related")

_JUDGEMENTS = """
CREATE TABLE IF NOT EXISTS judgements (
    query       TEXT NOT NULL,
    source      TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('relevant', 'irrelevant')),
    section     TEXT NOT NULL DEFAULT '',  -- the section a relevant hit must answer in; '' for any
    collection  TEXT NOT NULL DEFAULT '',  -- the set it was seeded from, or '' for a mark by hand
    note        TEXT NOT NULL DEFAULT '',
    judge       TEXT NOT NULL DEFAULT '',  -- the caller who recorded it
    ts          REAL NOT NULL,
    PRIMARY KEY (query, source, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_judgements_collection ON judgements(collection);
"""

#: Each ``search get``: who opened which hit for which query. No rank reads it until the journal replay shows
#: a usage rank beats the plain order (fleet.md R136 item 6).
_USAGE = """
CREATE TABLE IF NOT EXISTS usage (
    ts      REAL NOT NULL,
    scope   TEXT NOT NULL DEFAULT '',  -- the caller's scope, within its project
    query   TEXT NOT NULL DEFAULT '',
    source  TEXT NOT NULL,
    doc_id  TEXT NOT NULL,
    node    TEXT NOT NULL DEFAULT '',  -- the node holding the document; 'local' for this one
    project TEXT NOT NULL DEFAULT ''   -- the caller's project
);
CREATE INDEX IF NOT EXISTS idx_usage_scope ON usage(scope, ts);
"""
_USAGE_SCORE = """
CREATE TABLE IF NOT EXISTS usage_score (
    source TEXT NOT NULL, doc_id TEXT NOT NULL, node TEXT NOT NULL DEFAULT '', s REAL NOT NULL,
    PRIMARY KEY (source, doc_id, node));
CREATE INDEX IF NOT EXISTS idx_usage_score_rank ON usage_score(source, s DESC);
"""

#: v9 moves the documents to LanceDB: the SQLite copies, their text indexes and the embeddings go.
_TO_LANCE = """
DROP TRIGGER IF EXISTS documents_exact_ai;
DROP TRIGGER IF EXISTS documents_exact_ad;
DROP TRIGGER IF EXISTS documents_exact_au;
DROP TRIGGER IF EXISTS passages_words_ai;
DROP TRIGGER IF EXISTS passages_words_ad;
DROP TRIGGER IF EXISTS doc_values_ad;
DROP TABLE IF EXISTS documents_exact;
DROP TABLE IF EXISTS passages_words;
DROP TABLE IF EXISTS passages;
DROP TABLE IF EXISTS doc_values;
DROP TABLE IF EXISTS documents;
DROP TABLE IF EXISTS embeddings_fts;
DROP TABLE IF EXISTS embeddings;
ALTER TABLE sources ADD COLUMN cursor TEXT;
ALTER TABLE sources ADD COLUMN epoch TEXT;
ALTER TABLE sources ADD COLUMN layout TEXT;
UPDATE sources SET synced_at = NULL;
"""

SCHEMA_SQL = _SOURCES + _JUDGEMENTS + _USAGE + _USAGE_SCORE
MIGRATIONS = {(8, 9): _TO_LANCE}

_initialized = False


def init() -> None:
    """Idempotently create or migrate the search database."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION, migrations=MIGRATIONS)
        _initialized = True


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
    entries stay searchable as frozen. An unchanged registry writes nothing, because a query calls this and must not
    wait on a sweep's write lock.
    """
    now = time.time() if now is None else now
    stored = {r["name"]: r for r in conn.execute("SELECT name, mode, spec, present FROM sources")}
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
    """Every stored source, sorted by name."""
    return [{**dict(r), "spec": json.loads(r["spec"]), "present": bool(r["present"])}
            for r in conn.execute("SELECT * FROM sources ORDER BY name")]


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


def feed_state(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """``{cursor, epoch, layout}`` of a source; all ``None`` for one never read."""
    row = conn.execute("SELECT cursor, epoch, layout FROM sources WHERE name=?", (name,)).fetchone()
    return dict(row) if row else {"cursor": None, "epoch": None, "layout": None}


def save_feed_state(conn: sqlite3.Connection, name: str, cursor: str | None, epoch: str | None,
                    layout: str | None) -> None:
    conn.execute("UPDATE sources SET cursor=?, epoch=?, layout=? WHERE name=?", (cursor, epoch, layout, name))
    conn.commit()


# -- feedback --------------------------------------------------------------

#: ``usage_score`` counts a get as exp(LAMBDA * t), t in days since EPOCH, kept as a log so a score never overflows:
#: a get 30 days later weighs twice as much, so the sum ranks by recency-weighted use.
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
LAMBDA = math.log(2) / 30


def logaddexp(a: float, b: float) -> float:
    hi, lo = (a, b) if a >= b else (b, a)
    return hi + math.log1p(math.exp(lo - hi))


def log_use(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Append one get to the usage log and fold it into the document's ``usage_score``."""
    row = {"ts": time.time(), "project": "", "scope": "", "query": "", "node": "", **row}
    conn.execute("INSERT INTO usage (ts, project, scope, query, source, doc_id, node)"
                 " VALUES (:ts, :project, :scope, :query, :source, :doc_id, :node)", row)
    gain = LAMBDA * (row["ts"] - EPOCH) / 86400
    held = conn.execute("SELECT s FROM usage_score WHERE source=? AND doc_id=? AND node=?",
                        (row["source"], row["doc_id"], row["node"])).fetchone()
    conn.execute("INSERT INTO usage_score (source, doc_id, node, s) VALUES (?,?,?,?)"
                 " ON CONFLICT (source, doc_id, node) DO UPDATE SET s = excluded.s",
                 (row["source"], row["doc_id"], row["node"],
                  gain if held is None else logaddexp(held[0], gain)))
    conn.commit()


def judge(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    """Record a verdict on one hit for one query, replacing any earlier verdict on the same pair."""
    row = {"section": "", "collection": "", "note": "", "judge": "", "ts": time.time(), **row}
    conn.execute(
        "INSERT INTO judgements (query, source, doc_id, verdict, section, collection, note, judge, ts)"
        " VALUES (:query, :source, :doc_id, :verdict, :section, :collection, :note, :judge, :ts)"
        " ON CONFLICT (query, source, doc_id) DO UPDATE SET verdict=excluded.verdict,"
        " section=excluded.section, collection=excluded.collection, note=excluded.note,"
        " judge=excluded.judge, ts=excluded.ts", row)
    conn.commit()
    return row


def unjudge(conn: sqlite3.Connection, query: str, source: str, doc_id: str) -> int:
    gone = conn.execute("DELETE FROM judgements WHERE query=? AND source=? AND doc_id=?",
                        (query, source, doc_id)).rowcount
    conn.commit()
    return gone


def judgements(conn: sqlite3.Connection, collection: str = "", query: str = "") -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM judgements WHERE 1=1", []
    if collection:
        sql, params = sql + " AND collection=?", [*params, collection]
    if query:
        sql, params = sql + " AND query=?", [*params, query]
    return [dict(r) for r in conn.execute(sql + " ORDER BY collection, query, source, doc_id", params)]
