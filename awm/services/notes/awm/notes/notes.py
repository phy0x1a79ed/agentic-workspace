"""Application logic for the notes service.

Every note is a uuid-named ``.md`` file (canonical content) plus a DB row (the
title-as-path index + search indexes + trash state). Mutations keep the file,
the ``notes`` row, the FTS mirror, and the embedding consistent.

Surface split (mirrors the writing service):
  - **read** (MCP + CLI + HTTP): :func:`search`, :func:`get`, :func:`tree`,
    :func:`vocab_list`.
  - **write / maintenance** (browser via ``/svc/notes/fn/*`` + CLI + HTTP; kept
    off the agent MCP surface): :func:`create`, :func:`save`, :func:`trash`,
    :func:`restore`, :func:`purge`, :func:`vocab_add`, :func:`vocab_remove`.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db, index, rooms, store

_WORD = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------


def _row(
    conn: sqlite3.Connection,
    r: sqlite3.Row,
    *,
    include_content: bool = False,
    score: float | None = None,
    snippet: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": r["id"],
        "path": r["path"],
        "created": r["created"],
        "modified": r["modified"],
        "deleted_at": r["deleted_at"],
        "file_path": str(store.file_path(r["id"])),
    }
    if score is not None:
        out["score"] = score
    if include_content or snippet:
        # Prefer the live in-memory room over the (possibly lagging) on-disk
        # file so a reader — agent, CLI, another tab — sees unflushed edits.
        live = rooms.live_content(r["id"])
        text = live if live is not None else store.read(r["id"])
        if include_content:
            out["content"] = text
        if snippet:
            flat = re.sub(r"\s+", " ", text).strip()
            out["snippet"] = flat[:180]
    return out


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create(conn: sqlite3.Connection, *, path: str = "", content: str = "") -> dict[str, Any]:
    """Create a new note (uuid file + row). Called on the editor's first edit."""
    nid = str(uuid.uuid4())
    ts = db.now_iso()
    store.write(nid, content)
    chash = db.content_hash(content)
    db.upsert_note(
        conn,
        {
            "id": nid,
            "path": path.strip(),
            "created": ts,
            "modified": ts,
            "deleted_at": None,
            "content_hash": chash,
            "embedded_hash": None,
        },
    )
    db.fts_replace(conn, nid, path.strip(), content)
    _embed(conn, nid, content, chash)
    conn.commit()
    return get(conn, nid, include_content=True)


def save(
    conn: sqlite3.Connection,
    note_id: str,
    *,
    content: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Update a note's content and/or title-path. Re-indexes FTS + re-embeds
    when the content changed. No-op fields are left untouched."""
    r = db.get_note(conn, note_id)
    if r is None:
        raise ValueError(f"no such note: {note_id}")
    new_path = r["path"] if path is None else path.strip()
    if content is None:
        new_content = store.read(note_id)
        chash = r["content_hash"]
    else:
        new_content = content
        chash = db.content_hash(content)
        store.write(note_id, content)
    db.upsert_note(
        conn,
        {
            "id": note_id,
            "path": new_path,
            "created": r["created"],
            "modified": db.now_iso(),
            "deleted_at": r["deleted_at"],
            "content_hash": chash,
            "embedded_hash": r["embedded_hash"],
        },
    )
    db.fts_replace(conn, note_id, new_path, new_content)
    if chash != r["embedded_hash"]:
        _embed(conn, note_id, new_content, chash)
    conn.commit()
    # If a live room exists (a browser has this note open), reconcile it to this
    # out-of-band write so its collaborators and any reader stay consistent.
    if content is not None:
        rooms.sync_from_disk(note_id, new_content)
    return get(conn, note_id, include_content=False)


def get(conn: sqlite3.Connection, note_id: str, *, include_content: bool = True) -> dict[str, Any]:
    r = db.get_note(conn, note_id)
    if r is None:
        raise ValueError(f"no such note: {note_id}")
    return _row(conn, r, include_content=include_content)


def tree(conn: sqlite3.Connection, *, include_trashed: bool = True) -> dict[str, Any]:
    """Flat note list for the side panel to assemble into a path tree.

    Returns active notes and (optionally) trashed notes separately so the panel
    renders the Trash section distinctly. Content is not included — only the
    path/metadata the tree needs.
    """
    active = [
        _row(conn, r) for r in db.list_notes(conn, include_trashed=False)
    ]
    result: dict[str, Any] = {"active": active}
    if include_trashed:
        trashed = [
            _row(conn, r)
            for r in db.list_notes(conn, include_trashed=True)
            if r["deleted_at"] is not None
        ]
        result["trashed"] = trashed
    return result


def trash(conn: sqlite3.Connection, note_id: str) -> dict[str, Any]:
    """Soft-delete: move a note to the 30-day trash."""
    r = db.get_note(conn, note_id)
    if r is None:
        raise ValueError(f"no such note: {note_id}")
    conn.execute(
        "UPDATE notes SET deleted_at=?, modified=? WHERE id=?",
        (db.now_iso(), db.now_iso(), note_id),
    )
    conn.commit()
    rooms.drop(note_id)
    return {"trashed": note_id}


def restore(conn: sqlite3.Connection, note_id: str) -> dict[str, Any]:
    r = db.get_note(conn, note_id)
    if r is None:
        raise ValueError(f"no such note: {note_id}")
    conn.execute(
        "UPDATE notes SET deleted_at=NULL, modified=? WHERE id=?",
        (db.now_iso(), note_id),
    )
    conn.commit()
    return get(conn, note_id, include_content=False)


def purge(conn: sqlite3.Connection, note_id: str | None = None) -> dict[str, Any]:
    """Hard-delete. With ``note_id`` → purge that one note now; without → purge
    every trashed note past the 30-day TTL. Removes the file, row, FTS, embed."""
    if note_id is not None:
        _hard_delete(conn, note_id)
        conn.commit()
        return {"purged": [note_id]}
    return purge_expired(conn)


def purge_expired(conn: sqlite3.Connection) -> dict[str, Any]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.TRASH_TTL_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT id FROM notes WHERE deleted_at IS NOT NULL AND deleted_at < ?",
        (cutoff,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    for nid in ids:
        _hard_delete(conn, nid)
    conn.commit()
    return {"purged": ids}


def _hard_delete(conn: sqlite3.Connection, note_id: str) -> None:
    index.drop_embedding(conn, note_id)
    db.fts_delete(conn, note_id)
    db.delete_note_row(conn, note_id)
    store.remove(note_id)
    rooms.drop(note_id)


# ---------------------------------------------------------------------------
# Live collaboration — the in-memory room is authoritative between flushes.
# (Broadcast/emit is the hub_adapter's job; these just own the merge + state.)
# ---------------------------------------------------------------------------


def collab_open(conn: sqlite3.Connection, note_id: str) -> dict[str, Any]:
    """Join a note's room: returns the authoritative ``{version, content}`` the
    client adopts. The room is seeded from disk on first open."""
    r = db.get_note(conn, note_id)
    if r is None:
        raise ValueError(f"no such note: {note_id}")
    return rooms.snapshot(note_id)


def collab_edit(note_id: str, base_version: int, content: str) -> dict[str, Any]:
    """Merge a client edit into the room. Returns the new authoritative
    ``{version, content, changed}``; the caller fans it out to subscribers."""
    return rooms.apply_edit(note_id, int(base_version), content)


def _embed(conn: sqlite3.Connection, note_id: str, text: str, chash: str) -> None:
    """Embed a note's content and stamp the embedded hash. Content-free notes
    are skipped (nothing to embed) but still stamped so we don't retry them.
    Best-effort: see :func:`index.reembed` — a broken embedding stack leaves the
    stamp stale rather than failing the caller's write."""
    index.reembed(conn, note_id, text, chash)


# ---------------------------------------------------------------------------
# Search — keyword (FTS5) / fuzzy (difflib) / semantic (embeddings)
# ---------------------------------------------------------------------------


def _allowed_ids(conn: sqlite3.Connection, *, include_trashed: bool) -> list[str]:
    return [r["id"] for r in db.list_notes(conn, include_trashed=include_trashed)]


def _fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH string from free text: quote each word token and
    AND them (implicit). Sidesteps FTS5 syntax errors on punctuation."""
    toks = _WORD.findall(query.lower())
    return " ".join(f'"{t}"' for t in toks)


def _keyword_ids(conn: sqlite3.Connection, query: str) -> list[str]:
    match = _fts_query(query)
    if not match:
        return []
    rows = conn.execute(
        "SELECT note_id FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rank",
        (match,),
    ).fetchall()
    return [r["note_id"] for r in rows]


def _fuzzy_score(term_n: str, path_n: str, content_n: str) -> float:
    """Typo-tolerant score of ``term`` against a note's path + content.

    Best of: whole-path ratio, per-segment ratio, per-content-word ratio, plus a
    substring boost. Tuned for the 'jump to a note whose title I half-remember'
    case, with a light content fallback."""
    best = difflib.SequenceMatcher(None, term_n, path_n).ratio()
    for seg in re.split(r"[/\s]+", path_n):
        if seg:
            best = max(best, difflib.SequenceMatcher(None, term_n, seg).ratio())
    if term_n and (term_n in path_n or term_n in content_n):
        best = max(best, 0.85)
    for tok in set(_WORD.findall(content_n)):
        best = max(best, difflib.SequenceMatcher(None, term_n, tok).ratio())
        if best >= 0.99:
            break
    return best


def _fuzzy_ids(conn: sqlite3.Connection, term: str, allowed: list[str], threshold: float = 0.5) -> list[tuple[str, float]]:
    term_n = db.normalize(term)
    if not term_n:
        return []
    scored: list[tuple[str, float]] = []
    for nid in allowed:
        r = db.get_note(conn, nid)
        if r is None:
            continue
        s = _fuzzy_score(term_n, db.normalize(r["path"] or ""), store.read(nid).lower())
        if s >= threshold:
            scored.append((nid, round(s, 4)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


def search(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    fuzzy: str | None = None,
    semantic: str | None = None,
    k: int = 50,
    include_trashed: bool = False,
) -> dict[str, Any]:
    """Keyword / fuzzy / semantic search over notes.

    Any combination of the three inputs may be supplied. Results are merged in
    precedence order keyword → fuzzy → semantic, de-duplicated by note id, with
    the score from the mode that first matched. With none supplied, returns the
    most-recently-modified notes.
    """
    allowed = _allowed_ids(conn, include_trashed=include_trashed)
    allowed_set = set(allowed)

    ordered: list[tuple[str, float | None]] = []
    seen: set[str] = set()

    def _add(nid: str, score: float | None) -> None:
        if nid in seen or nid not in allowed_set:
            return
        seen.add(nid)
        ordered.append((nid, score))

    if query:
        for nid in _keyword_ids(conn, query):
            _add(nid, None)
    if fuzzy:
        for nid, s in _fuzzy_ids(conn, fuzzy, allowed):
            _add(nid, s)
    if semantic:
        for h in index.search_semantic(conn, semantic, limit=200):
            if h["score"] > 0.3:
                _add(h["source_id"], h["score"])

    if not (query or fuzzy or semantic):
        # Most-recently-modified listing.
        rows = sorted(
            (db.get_note(conn, nid) for nid in allowed),
            key=lambda r: r["modified"] or "",
            reverse=True,
        )
        ordered = [(r["id"], None) for r in rows]

    ordered = ordered[: int(k)]
    results = []
    for nid, score in ordered:
        r = db.get_note(conn, nid)
        if r is not None:
            results.append(_row(conn, r, score=score, snippet=True))
    return {"count": len(results), "results": results}


# ---------------------------------------------------------------------------
# Custom dictation vocabulary
# ---------------------------------------------------------------------------


def vocab_list(conn: sqlite3.Connection) -> dict[str, Any]:
    terms = db.vocab_terms(conn)
    return {"terms": terms}


def vocab_add(conn: sqlite3.Connection, term: str) -> dict[str, Any]:
    if not term or not term.strip():
        raise ValueError("term is required")
    db.vocab_add(conn, term)
    conn.commit()
    return vocab_list(conn)


def vocab_remove(conn: sqlite3.Connection, term: str) -> dict[str, Any]:
    db.vocab_remove(conn, term)
    conn.commit()
    return vocab_list(conn)
