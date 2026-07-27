"""Application logic for the precedence decision-archive service.

Operates on a service DB connection and returns JSON-serializable dicts (no
printing). Split by surface:

- **read / contribute** (MCP + CLI + HTTP): :func:`search`, :func:`get`,
  :func:`stats`, :func:`add`, :func:`note`, :func:`vote`. Agents both consume the
  archive *and* grow it — they can add decisions, attach change-signal notes, and
  vote on how useful an entry was.
- **curation** (CLI + HTTP only — gated off the MCP surface by the manifest
  ``surfaces`` field): :func:`edit`, :func:`supersede`, :func:`remove`,
  :func:`merge`, :func:`import_manifest`, :func:`embed`. Destructive / authority
  ops kept with the human curator.

Search is fielded + semantic, ranked by a *usefulness* blend (see
:mod:`config`), not raw cosine, with an explore/exploit knob and an impression
side effect that feeds the reputation loop.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, db, index


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts: str | None) -> datetime | None:
    """Best-effort parse of a stored timestamp (ISO, or YYYY-MM-DD). None if empty."""
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None
    for parse in (
        lambda v: datetime.fromisoformat(v),
        lambda v: datetime.strptime(v[:10], "%Y-%m-%d"),
    ):
        try:
            dt = parse(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _age_days(created: str | None, now: datetime) -> float | None:
    dt = _parse_ts(created)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 86400.0)


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------


def _row(
    conn: sqlite3.Connection,
    r: sqlite3.Row,
    *,
    breakdown: dict[str, float] | None = None,
    include_notes: bool = True,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": r["id"],
        "context": r["context"],
        "question": r["question"],
        "decision": r["decision"],
        "status": r["status"],
        "created": r["created"],
        "added": r["added"],
        "source": r["source"],
        "source_ref": r["source_ref"],
        "superseded_by": r["superseded_by"],
        "tags": db.tags_for(conn, r["id"]),
        "upvotes": r["upvotes"],
        "downvotes": r["downvotes"],
        "seen_count": r["seen_count"],
        "last_seen": r["last_seen"],
    }
    if breakdown is not None:
        out["score"] = breakdown["score"]
        out["ranking"] = breakdown
    if include_notes:
        out["notes"] = db.notes_for(conn, r["id"])
    return out


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _status_clause(status: str | None, include_superseded: bool) -> tuple[str, list[Any]]:
    if status:
        return "d.status = ?", [status]
    if include_superseded:
        return "d.status IN ('active','superseded')", []
    return "d.status = 'active'", []


def _filtered_ids(
    conn: sqlite3.Connection,
    *,
    tag: list[str] | None = None,
    status: str | None = None,
    after: str | None = None,
    before: str | None = None,
    include_superseded: bool = False,
) -> list[str]:
    where: list[str] = []
    params: list[Any] = []
    clause, cparams = _status_clause(status, include_superseded)
    where.append(clause)
    params.extend(cparams)
    if after:
        where.append("d.created >= ?")
        params.append(after)
    if before:
        where.append("d.created <= ?")
        params.append(before)
    for t in tag or []:
        where.append(
            "EXISTS (SELECT 1 FROM tags tg WHERE tg.decision_id=d.id AND tg.tag=?)"
        )
        params.append(t)
    clause_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT d.id FROM decisions d{clause_sql} ORDER BY d.created", params
    ).fetchall()
    return [r["id"] for r in rows]


def _keyword_ids(conn: sqlite3.Connection, query: str) -> list[str]:
    rows = conn.execute(
        "SELECT decision_id FROM decisions_fts WHERE decisions_fts MATCH ? ORDER BY rank",
        (query,),
    ).fetchall()
    return [r["decision_id"] for r in rows]


# ---------------------------------------------------------------------------
# Search — fielded semantic + keyword, ranked by usefulness
# ---------------------------------------------------------------------------


def _relevance_map(
    conn: sqlite3.Connection,
    *,
    context: str | None,
    question: str | None,
    decision: str | None,
    keyword: str | None,
    allowed: set[str],
) -> dict[str, float] | None:
    """Per-decision relevance in ~[0,1] over the allowed set, or None for listing mode.

    Each provided semantic field contributes its cosine (weighted, normalized by
    the provided fields' weights); a field with no counterpart embedding
    contributes 0. Keyword (FTS) hits contribute a flat relevance floor, combined
    by max with the semantic score. Returns None when neither a semantic field nor
    a keyword was supplied (caller falls back to a reputation/explore-ordered list).
    """
    provided = {
        f: q for f, q in (("context", context), ("question", question),
                          ("decision", decision)) if q and q.strip()
    }
    if not provided and not (keyword and keyword.strip()):
        return None

    rel: dict[str, float] = {}
    if provided:
        # Uniform per-field weight so a blended relevance stays in [0,1].
        total_w = float(len(provided))
        per_field: dict[str, dict[str, float]] = {}
        for field, q in provided.items():
            hits = index.search_field(conn, field, q, limit=config.SEMANTIC_LIMIT)
            per_field[field] = {h["source_id"]: h["score"] for h in hits}
        cand: set[str] = set()
        for m in per_field.values():
            cand |= set(m)
        for did in cand & allowed:
            s = sum(per_field[f].get(did, 0.0) for f in provided) / total_w
            rel[did] = s

    if keyword and keyword.strip():
        for did in _keyword_ids(conn, keyword):
            if did in allowed:
                rel[did] = max(rel.get(did, 0.0), config.KEYWORD_RELEVANCE)

    return rel


def search(
    conn: sqlite3.Connection,
    *,
    context: str | None = None,
    question: str | None = None,
    decision: str | None = None,
    keyword: str | None = None,
    tag: list[str] | None = None,
    status: str | None = None,
    after: str | None = None,
    before: str | None = None,
    k: int = config.DEFAULT_K,
    explore: float | None = None,
    include_superseded: bool = False,
) -> dict[str, Any]:
    """Fielded semantic + keyword search, ranked by the usefulness blend.

    ``context`` / ``question`` / ``decision`` are **independent optional** semantic
    queries mirroring the entry shape — supply any subset ("context like this,
    where the question is this"). ``keyword`` is an FTS5 query over the triple.
    ``tag`` (AND-ed), ``status``, ``after``/``before`` filter structurally.
    Superseded/retired excluded by default (``include_superseded`` re-admits
    superseded). ``explore`` in [0,1] rotates low-``seen_count`` entries up
    (default :data:`config.DEFAULT_EXPLORE`; pin 0 for the single most-trusted).

    Ranks the candidate set, returns the top ``k``, and — as a side effect —
    bumps ``seen_count`` + ``last_seen`` on the returned entries so the archive
    gathers the impressions the explore/exploit term feeds on.
    """
    if explore is None:
        explore = config.DEFAULT_EXPLORE
    explore = max(0.0, float(explore))

    allowed = _filtered_ids(
        conn, tag=tag, status=status, after=after, before=before,
        include_superseded=include_superseded,
    )
    allowed_set = set(allowed)
    if not allowed_set:
        return {"count": 0, "results": []}

    rel = _relevance_map(
        conn, context=context, question=question, decision=decision,
        keyword=keyword, allowed=allowed_set,
    )
    # Listing mode: no query → every allowed entry, relevance 0 (pure reputation/
    # recency/explore ordering). Query mode: only the semantic/keyword candidates.
    candidates = allowed if rel is None else list(rel.keys())
    rel = rel or {}

    now = datetime.now(timezone.utc)
    scored: list[tuple[str, dict[str, float], sqlite3.Row]] = []
    for did in candidates:
        r = db.get_decision(conn, did)
        if r is None:
            continue
        jitter = random.uniform(0, config.EXPLORE_JITTER) if explore else 0.0
        bd = config.usefulness(
            relevance=rel.get(did, 0.0),
            upvotes=r["upvotes"],
            downvotes=r["downvotes"],
            seen_count=r["seen_count"],
            age_days=_age_days(r["created"], now),
            explore=explore,
            jitter=jitter,
        )
        scored.append((did, bd, r))

    scored.sort(key=lambda t: t[1]["score"], reverse=True)
    top = scored[: int(k)]

    _bump_impressions(conn, [did for did, _, _ in top])

    return {
        "count": len(top),
        "results": [_row(conn, r, breakdown=bd) for _, bd, r in top],
    }


def _bump_impressions(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Increment seen_count + set last_seen on the returned ids.

    Best-effort: an impression bump must never make the read itself fail.
    """
    if not ids:
        return
    try:
        now = _now_iso()
        conn.executemany(
            "UPDATE decisions SET seen_count = seen_count + 1, last_seen = ? WHERE id = ?",
            [(now, did) for did in ids],
        )
        conn.commit()
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def get(conn: sqlite3.Connection, decision_id: str, *, bump: bool = False) -> dict[str, Any]:
    r = db.get_decision(conn, decision_id)
    if r is None:
        raise ValueError(f"no such decision: {decision_id}")
    if bump:
        _bump_impressions(conn, [decision_id])
        r = db.get_decision(conn, decision_id)
    return _row(conn, r)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = db.all_decisions(conn)
    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for r in rows:
        by_status[r["status"] or "unknown"] = by_status.get(r["status"] or "unknown", 0) + 1
        by_source[r["source"] or "unknown"] = by_source.get(r["source"] or "unknown", 0) + 1
    emb = conn.execute(
        "SELECT COUNT(DISTINCT source_id) FROM embeddings WHERE source_type IN (?,?,?)",
        tuple(config.SOURCE_TYPES.values()),
    ).fetchone()[0]
    notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    votes = conn.execute(
        "SELECT COALESCE(SUM(upvotes),0), COALESCE(SUM(downvotes),0) FROM decisions"
    ).fetchone()
    return {
        "decisions": len(rows),
        "embedded": emb,
        "notes": notes,
        "by_status": by_status,
        "by_source": by_source,
        "upvotes": votes[0],
        "downvotes": votes[1],
    }


# ---------------------------------------------------------------------------
# Contribute (agent-facing writes: add / note / vote)
# ---------------------------------------------------------------------------


def add(
    conn: sqlite3.Connection,
    *,
    context: str,
    question: str,
    decision: str,
    tag: list[str] | None = None,
    source: str = "agent",
    source_ref: str | None = None,
    created: str | None = None,
) -> dict[str, Any]:
    """Add a decision (agents contribute live). Embeds all three fields.

    Id is derived from ``source_ref`` when given (import idempotency) else from the
    triple (identical-content agent adds collapse to one row).
    """
    for name, val in (("context", context), ("question", question), ("decision", decision)):
        if not val or not str(val).strip():
            raise ValueError(f"{name} is required and must be non-empty")
    if source and source not in config.SOURCES:
        raise ValueError(f"unknown source {source!r}; allowed: {', '.join(config.SOURCES)}")

    did = db.stable_id(source_ref) if source_ref else db.stable_id(context, question, decision)
    chash = db.content_hash(context, question, decision)
    now = _now_iso()
    existing = db.get_decision(conn, did)
    row = {
        "id": did,
        "context": context,
        "question": question,
        "decision": decision,
        "created": created or (existing["created"] if existing else now),
        "added": existing["added"] if existing else now,
        "status": existing["status"] if existing else "active",
        "source": source,
        "source_ref": source_ref,
        "content_hash": chash,
    }
    db.upsert_decision(conn, row)
    db.fts_replace(conn, did, context, question, decision)
    for t in tag or []:
        db.set_tag(conn, did, t)
    index.embed_decision(conn, did, context, question, decision)
    conn.execute("UPDATE decisions SET embedded_hash=? WHERE id=?", (chash, did))
    conn.commit()
    return get(conn, did)


def note(
    conn: sqlite3.Connection,
    decision_id: str,
    *,
    body: str,
    kind: str = "comment",
    author: str = "agent",
) -> dict[str, Any]:
    """Attach a note — a signal that something changed that may affect the outcome.

    Notes are separate content (not part of the searchable triple), so this does
    not touch the decision's embeddings.
    """
    if db.get_decision(conn, decision_id) is None:
        raise ValueError(f"no such decision: {decision_id}")
    if kind not in config.NOTE_KINDS:
        raise ValueError(f"unknown note kind {kind!r}; allowed: {', '.join(config.NOTE_KINDS)}")
    if not body or not body.strip():
        raise ValueError("note body is required and must be non-empty")
    note_id = db.stable_id(decision_id, kind, body, _now_iso())
    db.add_note(conn, note_id, decision_id, body, kind, author, _now_iso())
    conn.commit()
    return get(conn, decision_id)


def vote(conn: sqlite3.Connection, decision_id: str, *, direction: str) -> dict[str, Any]:
    """Up/down vote a decision — feeds the reputation term of the usefulness blend."""
    if db.get_decision(conn, decision_id) is None:
        raise ValueError(f"no such decision: {decision_id}")
    d = str(direction).lower()
    if d in ("up", "+1", "1", "upvote"):
        conn.execute("UPDATE decisions SET upvotes = upvotes + 1 WHERE id=?", (decision_id,))
    elif d in ("down", "-1", "downvote"):
        conn.execute("UPDATE decisions SET downvotes = downvotes + 1 WHERE id=?", (decision_id,))
    else:
        raise ValueError(f"direction must be up|down, got {direction!r}")
    conn.commit()
    return get(conn, decision_id)


# ---------------------------------------------------------------------------
# Curation (CLI + HTTP only: edit / supersede / remove / merge / import / embed)
# ---------------------------------------------------------------------------


def edit(
    conn: sqlite3.Connection,
    decision_id: str,
    *,
    context: str | None = None,
    question: str | None = None,
    decision: str | None = None,
    created: str | None = None,
    add_tag: list[str] | None = None,
    rm_tag: list[str] | None = None,
) -> dict[str, Any]:
    """Edit a decision's fields / tags. Re-embeds all three fields if any changed."""
    r = db.get_decision(conn, decision_id)
    if r is None:
        raise ValueError(f"no such decision: {decision_id}")
    new_ctx = context if context is not None else r["context"]
    new_q = question if question is not None else r["question"]
    new_dec = decision if decision is not None else r["decision"]
    changed = (new_ctx, new_q, new_dec) != (r["context"], r["question"], r["decision"])
    chash = db.content_hash(new_ctx, new_q, new_dec)
    db.upsert_decision(conn, {
        "id": decision_id,
        "context": new_ctx, "question": new_q, "decision": new_dec,
        "created": created if created is not None else r["created"],
        "content_hash": chash,
    })
    for t in rm_tag or []:
        db.clear_tag(conn, decision_id, t)
    for t in add_tag or []:
        db.set_tag(conn, decision_id, t)
    if changed:
        db.fts_replace(conn, decision_id, new_ctx, new_q, new_dec)
        index.embed_decision(conn, decision_id, new_ctx, new_q, new_dec)
        conn.execute("UPDATE decisions SET embedded_hash=? WHERE id=?", (chash, decision_id))
    conn.commit()
    return get(conn, decision_id)


def supersede(
    conn: sqlite3.Connection,
    decision_id: str,
    *,
    by: str | None = None,
    status: str = "superseded",
    note_body: str | None = None,
) -> dict[str, Any]:
    """Retire/supersede a decision (status flips; optional pointer to its replacement).

    ``status`` is ``superseded`` (default) or ``retired``; ``by`` optionally records
    the id of the replacing decision. A supersede note is attached when ``note_body``
    is given (kind ``supersede``).
    """
    if db.get_decision(conn, decision_id) is None:
        raise ValueError(f"no such decision: {decision_id}")
    if status not in ("superseded", "retired"):
        raise ValueError("status must be superseded|retired")
    if by is not None and db.get_decision(conn, by) is None:
        raise ValueError(f"no such replacement decision: {by}")
    conn.execute(
        "UPDATE decisions SET status=?, superseded_by=? WHERE id=?",
        (status, by, decision_id),
    )
    if note_body:
        nid = db.stable_id(decision_id, "supersede", note_body, _now_iso())
        db.add_note(conn, nid, decision_id, note_body, "supersede", "curator", _now_iso())
    conn.commit()
    return get(conn, decision_id)


def remove(conn: sqlite3.Connection, decision_id: str) -> dict[str, Any]:
    """Hard-delete a decision (row, notes, tags, FTS, and all three embeddings)."""
    if db.get_decision(conn, decision_id) is None:
        raise ValueError(f"no such decision: {decision_id}")
    index.drop_decision(conn, decision_id)
    db.delete_decision(conn, decision_id)
    conn.commit()
    return {"removed": decision_id}


def merge(conn: sqlite3.Connection, keeper: str, dups: list[str]) -> dict[str, Any]:
    """Absorb duplicate entries into a keeper (folds tags, notes, votes, seen).

    Exists specifically to clean up duplicate agent-added entries: the keeper
    accumulates the dups' tags, notes, votes and seen-counts, then each dup is
    hard-removed (row + embeddings).
    """
    if db.get_decision(conn, keeper) is None:
        raise ValueError(f"no such keeper: {keeper}")
    merged = 0
    for did in dups:
        if did == keeper or db.get_decision(conn, did) is None:
            continue
        r = db.get_decision(conn, did)
        for t in db.tags_for(conn, did):
            db.set_tag(conn, keeper, t)
        conn.execute("UPDATE notes SET decision_id=? WHERE decision_id=?", (keeper, did))
        conn.execute(
            "UPDATE decisions SET upvotes=upvotes+?, downvotes=downvotes+?, "
            "seen_count=seen_count+? WHERE id=?",
            (r["upvotes"], r["downvotes"], r["seen_count"], keeper),
        )
        index.drop_decision(conn, did)
        db.fts_delete(conn, did)
        conn.execute("DELETE FROM tags WHERE decision_id=?", (did,))
        conn.execute("DELETE FROM decisions WHERE id=?", (did,))
        merged += 1
    conn.commit()
    return {"keeper": keeper, "merged": merged}


def embed(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, Any]:
    """Embed new/changed decisions (or all, with ``force``). Loads the model once."""
    rows = db.all_decisions(conn)
    todo = [r for r in rows if force or r["embedded_hash"] != r["content_hash"]]
    for r in todo:
        index.embed_decision(conn, r["id"], r["context"], r["question"], r["decision"])
        conn.execute(
            "UPDATE decisions SET embedded_hash=? WHERE id=?", (r["content_hash"], r["id"])
        )
        conn.commit()
    return {"embedded": len(todo)}


def import_manifest(
    conn: sqlite3.Connection, *, manifest_path: str, embed_after: bool = True
) -> dict[str, Any]:
    """Load a curated staging-manifest JSON of decisions into the service DB.

    Manifest shape: ``{"decisions": [ {context, question, decision, created?,
    source?, source_ref?, tags?[], notes?[{body, kind?, author?, created?}]} ]}``.
    Idempotent on a stable id (hash of ``source_ref``, else the triple) so a re-run
    updates in place rather than duplicating. Embeds new/changed entries afterward
    unless ``embed_after`` is False.
    """
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    entries = data.get("decisions", data if isinstance(data, list) else [])
    now = _now_iso()
    imported = changed = 0
    for e in entries:
        ctx, q, dec = e.get("context"), e.get("question"), e.get("decision")
        if not (ctx and q and dec):
            continue
        source_ref = e.get("source_ref")
        did = db.stable_id(source_ref) if source_ref else db.stable_id(ctx, q, dec)
        chash = db.content_hash(ctx, q, dec)
        prev = db.get_decision(conn, did)
        db.upsert_decision(conn, {
            "id": did,
            "context": ctx, "question": q, "decision": dec,
            "created": e.get("created") or (prev["created"] if prev else now),
            "added": prev["added"] if prev else now,
            "status": e.get("status") or (prev["status"] if prev else "active"),
            "superseded_by": e.get("superseded_by"),
            "source": e.get("source") or "manual",
            "source_ref": source_ref,
            "content_hash": chash,
        })
        db.fts_replace(conn, did, ctx, q, dec)
        for t in e.get("tags", []) or []:
            db.set_tag(conn, did, t)
        for n in e.get("notes", []) or []:
            nb = n.get("body")
            if not nb:
                continue
            nkind = n.get("kind", "comment")
            nid = db.stable_id(did, nkind, nb)
            db.add_note(conn, nid, did, nb, nkind, n.get("author", "curator"),
                        n.get("created") or now)
        imported += 1
        if prev is None or prev["content_hash"] != chash:
            changed += 1
    conn.commit()
    embedded = embed(conn)["embedded"] if embed_after else 0
    return {"imported": imported, "changed": changed, "embedded": embedded}
