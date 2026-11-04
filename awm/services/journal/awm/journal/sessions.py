"""Who wrote a journal, what it cites, and what each session read (sessions.md R14, R15, R17).

A journal post carries ``meta.session``, the R9 id of the session it covers. It
defaults to the posting session. A debrief journal's ``reply_to`` lists the
journals that shaped the work, and points only at journal posts. An old-style
``meta.cites`` folds into that list, its reasons kept as ``meta.cite_reasons``. Every journal
that ``fetch`` hands a session lands in ``session_reads``, the candidate list
the agent picks its citations from at debrief.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from awm.gatewayclient import ids
from awm.journal import kinds
from awm.journal.dao import JournalDAO
from awm.journal.refs import now_ms

MAX_REASON = 200



def is_journal(tag_ids: Any) -> bool:
    return kinds.JOURNAL in (tag_ids or ())


def stamp(meta: Any, caller: str | None) -> Any:
    """``meta`` with ``session`` in R9 form, defaulting to the posting session.

    A caller names another session only to journal for it (R17). Then the
    posting session is kept as ``posted_by``.
    """
    if not isinstance(meta, dict):
        return {"session": caller} if caller else meta
    meta = dict(meta)
    named = meta.pop("session", None)
    if named in (None, ""):
        return {**meta, "session": caller} if caller else meta
    session = ids.to_r9(named)
    if session is None:
        raise ValueError(f"meta.session {named!r} is not a session id")
    meta["session"] = session
    if caller and caller != session:
        meta["posted_by"] = caller
    return meta


def fold_cites(meta: Any, tag_ids: list[str]) -> tuple[Any, list[str]]:
    """Fold an old-style ``meta.cites`` into citation ids for ``reply_to``.

    Each reason it gives stays as ``meta.cite_reasons``; a citation needs none
    (sessions.md R15). Returns ``meta`` without ``cites`` and the cited ids.
    """
    if not isinstance(meta, dict) or "cites" not in meta:
        return meta, []
    meta = dict(meta)
    cites = meta.pop("cites")
    if cites in (None, []):
        return meta, []
    if not is_journal(tag_ids):
        raise ValueError("only a debrief journal cites (sessions.md R14)")
    if not isinstance(cites, list):
        raise ValueError("meta.cites is a list of {journal}; pass the ids in reply_to instead")
    ids, reasons = [], {}
    for c in cites:
        if not isinstance(c, dict) or not isinstance(c.get("journal"), str):
            raise ValueError("each meta.cites entry is {journal: <post id>}; use reply_to")
        if c["journal"] not in ids:
            ids.append(c["journal"])
        reason = c.get("reason")
        if isinstance(reason, str) and reason.strip():
            reasons[c["journal"]] = " ".join(reason.split())[:MAX_REASON]
    if reasons:
        meta["cite_reasons"] = {**(meta.get("cite_reasons") or {}), **reasons}
    return meta, ids


def record_reads(session: str | None, posts: Iterable[Any]) -> int:
    """Note each journal in ``posts`` as read by ``session``. Its own journals are skipped."""
    if not session:
        return 0
    ids = [p.id for p in posts
           if is_journal(getattr(p, "tag_ids", None)) and (p.meta or {}).get("session") != session]
    if not ids:
        return 0
    now = now_ms()
    JournalDAO().executemany(
        "INSERT INTO session_reads (session, post_id, first_read, last_read) VALUES (?,?,?,?)"
        " ON CONFLICT (session, post_id) DO UPDATE SET last_read=excluded.last_read, times=times+1",
        [(session, pid, now, now) for pid in dict.fromkeys(ids)])
    return len(ids)


def reads(session: str, limit: int = 200) -> list[dict]:
    """The journals ``session`` read, newest read first, with whose session each covers."""
    rows = JournalDAO().query_all(
        "SELECT r.post_id, r.first_read, r.last_read, r.times, p.owner_project, p.owner_scope,"
        " p.meta, p.body FROM session_reads r JOIN scope_posts p ON p.id = r.post_id"
        " WHERE r.session=? ORDER BY r.last_read DESC LIMIT ?", (session, int(limit)))
    out = []
    for r in rows:
        meta = _meta(r["meta"])
        lines = (r["body"] or "").strip().splitlines()
        out.append({
            "journal": r["post_id"], "project": r["owner_project"], "scope": r["owner_scope"],
            "title": meta.get("title") or (lines[0][:120] if lines else ""),
            "session": meta.get("session"),
            "first_read": r["first_read"], "last_read": r["last_read"], "times": r["times"],
        })
    return out


def cites_of(session: str) -> list[dict]:
    """The union of the citations of every journal ``session`` wrote (R14)."""
    from awm.journal import lineage
    out, seen = [], set()
    for c in lineage.cited_by_session(session):
        if c["journal"] not in seen:
            seen.add(c["journal"])
            out.append(c)
    return out


def _meta(raw: Any) -> dict:
    try:
        meta = json.loads(raw or "{}")
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}
