"""One-time migration: historical ``corpus.db`` → the writing service's own DB.

The original corpus lived as a SQLite DB *next to* the text files in the
self-improvement project data dir. This brings it into the service-owned DB
(``AWM_DIR/services/writing/writing.db``), storing full text in-DB and copying
the embeddings **verbatim** (same all-MiniLM vectors — no re-embedding).

Idempotent: keyed on sample id, so re-running is safe. Preserves the original
ids (full Drive ids) so dedup ``keeper``/``dup_of`` references stay valid.

Usage:
    python -m awm.writing.seed                 # default corpus dir
    python -m awm.writing.seed /path/to/writing_samples
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from . import config, dao, db


def _old_columns(old: sqlite3.Connection) -> set[str]:
    return {r[1] for r in old.execute("PRAGMA table_info(samples)").fetchall()}


def migrate(corpus_dir: str | Path | None = None) -> dict[str, int]:
    root = Path(corpus_dir) if corpus_dir else config.default_corpus_dir()
    old_db = root / "corpus.db"
    if not old_db.exists():
        raise FileNotFoundError(f"no corpus.db at {old_db}")

    dao.init()
    new = dao.connect()
    old = sqlite3.connect(str(old_db))
    old.row_factory = sqlite3.Row

    has_content = "content" in _old_columns(old)

    n_samples = n_tags = n_emb = n_missing = 0

    # ---- samples (+ content read from the text files) --------------------
    for r in old.execute("SELECT * FROM samples").fetchall():
        sid = r["id"]
        if has_content and r["content"]:
            text = r["content"]
        else:
            path = root / r["file"]
            if not path.exists():
                n_missing += 1
                text = ""
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
        row = {
            "id": sid,
            "name": r["name"],
            "file": r["file"],
            "type": r["type"],
            "created": r["created"],
            "modified": r["modified"],
            "wordcount": r["wordcount"],
            "note": r["note"],
            "web_link": r["web_link"],
            "content": text,
            "content_hash": r["content_hash"],
            "embedded_hash": r["embedded_hash"],
            "grade": r["grade"],
            "dup_of": r["dup_of"],
            "dup_cluster": r["dup_cluster"],
        }
        db.upsert_sample(new, row)
        db.fts_replace(new, sid, r["name"], r["note"] or "", text)
        n_samples += 1

    # ---- tags -----------------------------------------------------------
    for t in old.execute("SELECT sample_id, facet, value FROM tags").fetchall():
        new.execute(
            "INSERT OR IGNORE INTO tags (sample_id, facet, value) VALUES (?,?,?)",
            (t["sample_id"], t["facet"], t["value"]),
        )
        n_tags += 1

    # ---- embeddings (copied verbatim — same model/vectors) --------------
    for e in old.execute(
        "SELECT source_type, source_id, chunk_text, embedding, updated_at "
        "FROM embeddings WHERE source_type=?", (config.SOURCE_TYPE,)
    ).fetchall():
        new.execute(
            "INSERT INTO embeddings (source_type, source_id, chunk_text, embedding, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(source_type, source_id) DO UPDATE SET "
            "  chunk_text=excluded.chunk_text, embedding=excluded.embedding, "
            "  updated_at=excluded.updated_at",
            (e["source_type"], e["source_id"], e["chunk_text"], e["embedding"], e["updated_at"]),
        )
        n_emb += 1

    new.commit()
    old.close()
    new.close()
    return {"samples": n_samples, "tags": n_tags, "embeddings": n_emb, "missing_files": n_missing}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    corpus_dir = argv[0] if argv else None
    counts = migrate(corpus_dir)
    print(
        f"writing.seed: migrated {counts['samples']} samples, {counts['tags']} tags, "
        f"{counts['embeddings']} embeddings"
        + (f" ({counts['missing_files']} missing text files → empty content)"
           if counts["missing_files"] else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
