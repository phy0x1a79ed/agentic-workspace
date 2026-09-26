"""Application logic for the notes service.

Every note is a uuid-named ``.md`` file (canonical content) plus a DB row (the
title-as-path index + search indexes + trash state). Mutations keep the file,
the ``notes`` row, the FTS mirror, and the embedding consistent.

Writing a note is one operation with one guard, wherever it comes from:
:func:`save` and every checkout verb land through :func:`awm.notes.rooms.land`,
which refuses a write composed against a note that has since moved. The only
writer that does not is the browser's own keystroke stream, which owns the room
it is writing to. See :mod:`awm.notes.checkout` for the contract an agent works
through, and why it exists.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import checkout as checkout_mod
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
    # Prefer the live in-memory room over the (possibly lagging) on-disk file so
    # a reader — agent, CLI, another tab — sees unflushed edits.
    text = rooms.live_text(r["id"])
    out: dict[str, Any] = {
        "id": r["id"],
        "path": r["path"],
        "created": r["created"],
        "modified": r["modified"],
        "deleted_at": r["deleted_at"],
        "file_path": str(store.file_path(r["id"])),
        # The revision of the text above. Hand it back as `base_rev` on save,
        # or take a checkout, and a write cannot silently land on top of
        # someone else's.
        "rev": db.text_rev(text),
    }
    if rooms.file_diverged(r["id"]):
        # The condition that makes an out-of-band file write invisible. Saying
        # so here is what turns it from a mystery into a fact.
        out["stale_file"] = True
        out["warning"] = (
            "this note is open in an editor and its file is behind the live "
            "text; read `content` here, and write through notes checkout/merge "
            "rather than the file"
        )
    if score is not None:
        out["score"] = score
    if include_content:
        out["content"] = text
    if snippet:
        flat = re.sub(r"\s+", " ", text).strip()
        out["snippet"] = flat[:180]
    return out


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create(
    conn: sqlite3.Connection,
    *,
    path: str = "",
    content: str = "",
    checkout: bool = False,
    author: str = "agent",
) -> dict[str, Any]:
    """Create a new note (uuid file + row). Called on the editor's first edit.

    Nothing exists to race here, so this is the one write with no guard. With
    ``checkout`` it also hands back a working copy of the new note, so "make
    this and fill it in" is one flow rather than two idioms.
    """
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
    out = get(conn, nid, include_content=True)
    if checkout:
        out["checkout"] = take_checkout(conn, nid, author=author)
    return out


def save(
    conn: sqlite3.Connection,
    note_id: str,
    *,
    content: str | None = None,
    path: str | None = None,
    base_rev: str | None = None,
    author: str = "agent",
) -> dict[str, Any]:
    """Update a note's content and/or title-path.

    A content save is a **merge, not a replacement**. The caller's text is
    reconciled against whatever the note holds now — the live room if a browser
    has it open — using the same three-way merge :func:`awm.notes.checkout`
    uses, and the result lands atomically. So an edit composed against an older
    copy folds in rather than erasing what happened since, and a genuine
    conflict is refused with the checkout named, never guessed at.

    ``base_rev`` is the ``rev`` this text was composed against, from any read
    verb; it makes the merge base exact. Without it the base is taken to be the
    note's file, which is right for the case that motivates it — a caller that
    read the file, edited it, and is writing it back.
    """
    r = db.get_note(conn, note_id)
    if r is None:
        raise ValueError(f"no such note: {note_id}")
    new_path = r["path"] if path is None else path.strip()

    if content is None:
        # Title-only. The body is untouched, but the keyword index is rebuilt
        # from (path, body) as one row — so it must use the *live* body, or
        # renaming an open note regresses its search text to the file's copy
        # until the next flush.
        new_content = rooms.live_text(note_id)
        chash = r["content_hash"]
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
        conn.commit()
        return get(conn, note_id, include_content=False)

    live = rooms.live_text(note_id)
    live_rev = db.text_rev(live)
    base = rooms.text_at_rev(note_id, base_rev) if base_rev else store.read(note_id)
    if base is None:
        raise checkout_mod.Behind(
            f"the revision this save was composed against ({base_rev}) is no "
            f"longer reachable; take a checkout of {note_id} and reconcile there"
        )

    if db.text_rev(base) == live_rev:
        landed_text = content                      # nothing moved under us
        conflicts = 0
    else:
        landed_text, conflicts = checkout_mod.merge3(
            base, content, live, labels=("your save", "common base", "live note"),
        )
    if conflicts:
        raise checkout_mod.Conflicted(
            f"{conflicts} conflict(s) between this save and the live note; "
            f"take a checkout of {note_id}, run update, resolve by hand, then merge"
        )

    if new_path != r["path"]:
        db.upsert_note(
            conn,
            {
                "id": note_id,
                "path": new_path,
                "created": r["created"],
                "modified": db.now_iso(),
                "deleted_at": r["deleted_at"],
                "content_hash": r["content_hash"],
                "embedded_hash": r["embedded_hash"],
            },
        )
        conn.commit()
    landed = rooms.land(conn, note_id, landed_text, live_rev)
    out = get(conn, note_id, include_content=False)
    out["merged"] = landed_text != content
    out["version"] = landed["version"]
    return out


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
    # A checkout of a note that no longer exists can never land; leaving it
    # listed would be an invitation to try.
    for handle in checkout_mod.registry().list(note_id):
        checkout_mod.registry().discard(handle.id)


# ---------------------------------------------------------------------------
# Checkouts — the write path for anyone who is not the browser. The contract
# and its reasoning live in :mod:`awm.notes.checkout`; these only add the two
# things that module deliberately does not know about: the DB row (does this
# note exist?) and the connection a merge needs to land through.
# ---------------------------------------------------------------------------


def take_checkout(conn: sqlite3.Connection, note_id: str, *, author: str = "agent") -> dict[str, Any]:
    r = db.get_note(conn, note_id)
    if r is None:
        raise ValueError(f"no such note: {note_id}")
    handle = checkout_mod.registry().checkout(note_id, author, note_path=r["path"])
    out = handle.as_dict()
    out["path"] = str(checkout_mod.registry().file_path(handle.id))
    out["note"] = (
        "edit the file at `path`, then merge. The live note is untouched until "
        "you do, and your copy is untouched by whoever else is editing it."
    )
    return out


def list_checkouts(note_id: str | None = None) -> dict[str, Any]:
    handles = checkout_mod.registry().list(note_id)
    return {"count": len(handles), "checkouts": [h.as_dict() for h in handles]}


def checkout_path(handle: str) -> dict[str, Any]:
    reg = checkout_mod.registry()
    return {"handle": handle, "note_id": reg.note_id_of(handle),
            "path": str(reg.file_path(handle))}


def checkout_read(handle: str) -> dict[str, Any]:
    reg = checkout_mod.registry()
    return {"handle": handle, "note_id": reg.note_id_of(handle),
            "content": reg.read(handle)}


def checkout_write(handle: str, content: str) -> dict[str, Any]:
    return checkout_mod.registry().write(handle, content)


def checkout_status(handle: str) -> dict[str, Any]:
    return checkout_mod.registry().status(handle)


def checkout_update(handle: str) -> dict[str, Any]:
    return checkout_mod.registry().update(handle)


def checkout_resolve(handle: str) -> dict[str, Any]:
    return checkout_mod.registry().resolve(handle)


def checkout_merge(conn: sqlite3.Connection, handle: str, *, keep: bool = False) -> dict[str, Any]:
    return checkout_mod.registry().merge(conn, handle, keep=keep)


def checkout_discard(handle: str) -> dict[str, Any]:
    return checkout_mod.registry().discard(handle)


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


def reindex(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, Any]:
    """Re-embed notes whose content changed since their last embed.

    The counterpart to :func:`_embed`'s best-effort failure: writes made while
    the embedding stack was unavailable (or interrupted mid-flush) leave
    ``embedded_hash`` stale, and nothing retries them until the note is next
    edited — so those notes stay invisible to semantic search indefinitely.
    ``force`` re-embeds every note, for a model change or a corrupted table.

    Probes up front rather than discovering the same failure once per note, so
    a stackless node gets one actionable error instead of N swallowed ones.
    """
    p = index.probe()
    if not p["available"]:
        raise index.EmbeddingsUnavailable(
            f"cannot reindex: missing {', '.join(p['missing'])} — "
            "run awm/services/notes/install.sh on this node"
        )
    # An open note's authoritative content is its in-memory room, not the file,
    # so flush first — otherwise a reindex would embed a body the user has
    # already edited past, and stamp it as current.
    rooms.flush_all(conn)
    embedded, failed = 0, 0
    for r in db.list_notes(conn, include_trashed=False):
        if not force and r["embedded_hash"] == r["content_hash"]:
            continue
        nid = r["id"]
        if index.reembed(conn, nid, store.read(nid), r["content_hash"]):
            embedded += 1
        else:
            failed += 1
        conn.commit()   # per row: a mid-run failure keeps the progress made
    stale = conn.execute(
        "SELECT COUNT(*) FROM notes WHERE deleted_at IS NULL "
        "AND (embedded_hash IS NULL OR embedded_hash <> content_hash)"
    ).fetchone()[0]
    return {"embedded": embedded, "failed": failed, "stale_remaining": stale}


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

    When the semantic stack isn't installed on this node the semantic leg falls
    back to fuzzy matching on the same string and the payload carries a
    ``degraded`` block — never a silent empty result, which would read as "no
    such note" rather than "this node cannot rank by meaning".
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
    degraded: dict[str, Any] | None = None
    if semantic:
        try:
            hits = index.search_semantic(conn, semantic, limit=200)
        except index.EmbeddingsUnavailable:
            degraded = index.degraded_marker("notes", fallback="fuzzy")
            # Fall back to the dependency-free difflib matcher over the same
            # string — a genuinely useful substitute for "find the note I mean".
            hits = []
            for nid, s in _fuzzy_ids(conn, semantic, allowed):
                _add(nid, s)
        for h in hits:
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
    out: dict[str, Any] = {"count": len(results), "results": results}
    if degraded:
        out["degraded"] = degraded
    return out


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
