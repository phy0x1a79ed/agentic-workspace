"""First-start copy of the post log from the scopes database.

Posts lived in the scopes service's ``scope_posts`` table before the
journal service existed. When the journal's own log is empty, this copies that
table and the subscriber table across. It backs up the journal database first,
copies with ``INSERT OR IGNORE`` so a rerun adds nothing, and never writes to
the scopes database, which keeps its tables as they were. Each post's ``kind``
becomes its tag, by the rule the schema migration uses.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from awm.journal import kinds
from awm.persistence.databases import get_connection_at, service_db_path

log = logging.getLogger("awm.journal.migrate")

_POST_COLS = "id, owner_project, owner_scope, author, body, meta, ts"
_SUB_COLS = "owner_project, owner_scope, guest_kind, guest_ref, display_name, joined_at"
_BATCH = 500


def _backup(db: Path) -> Path | None:
    if not db.exists():
        return None
    target = db.with_name(f"{db.name}.bak-premigrate-{time.strftime('%Y%m%d-%H%M%S')}")
    src = sqlite3.connect(db)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _kind_tag(conn: sqlite3.Connection, kind: str | None) -> str:
    """The tag id for an old kind, adding a migrated-kind tag for one the code does not name."""
    name = (kind or "").strip().lower()
    if not name:
        return kinds.MESSAGE
    if name in kinds.KIND_TO_TAG:
        return kinds.KIND_TO_TAG[name]
    if name in kinds.FOLDED_KINDS:
        return kinds.FOLDED_KINDS[name]
    row = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
    if row is not None:
        return row[0]
    tid = kinds.MIGRATED_PREFIX + name
    conn.execute("INSERT OR IGNORE INTO tags (id, name, description, origin, version, created_at,"
                 " updated_at) VALUES (?,?,?,'',1,0,0)", (tid, name, kinds.MIGRATED_DESCRIPTION))
    return tid


def migrate_from_scopes(*, source: Path | None = None, target: Path | None = None) -> dict:
    """Copy ``scope_posts`` and ``scope_subscribers`` from the scopes DB; returns what happened."""
    source = source or service_db_path("scopes")
    target = target or service_db_path("journal")
    if not source.exists():
        return {"status": "no-source"}
    dst = get_connection_at(target)
    try:
        if dst.execute("SELECT 1 FROM scope_posts LIMIT 1").fetchone() is not None:
            return {"status": "skipped", "reason": "journal log is not empty"}
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        try:
            if not _has_table(src, "scope_posts"):
                return {"status": "no-source"}
            backup = _backup(target)
            posts = subs = 0
            with dst:
                cur = src.execute(f"SELECT {_POST_COLS}, kind FROM scope_posts ORDER BY ts, id")
                while rows := cur.fetchmany(_BATCH):
                    dst.executemany(
                        f"INSERT OR IGNORE INTO scope_posts ({_POST_COLS}) VALUES (?,?,?,?,?,?,?)",
                        [tuple(r)[:-1] for r in rows])
                    dst.executemany("INSERT OR IGNORE INTO post_tags (post_id, tag_id) VALUES (?,?)",
                                    [(r["id"], _kind_tag(dst, r["kind"])) for r in rows])
                    posts += len(rows)
                if _has_table(src, "scope_subscribers"):
                    rows = src.execute(f"SELECT {_SUB_COLS} FROM scope_subscribers").fetchall()
                    dst.executemany(
                        f"INSERT OR IGNORE INTO scope_subscribers ({_SUB_COLS}) VALUES (?,?,?,?,?,?)",
                        [tuple(r) for r in rows])
                    subs = len(rows)
        finally:
            src.close()
    finally:
        dst.close()
    log.info("migrated %d posts and %d subscribers from %s", posts, subs, source)
    return {"status": "migrated", "posts": posts, "subscribers": subs,
            "backup": str(backup) if backup else None}
