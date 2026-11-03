"""Who wrote a journal, what it cites, and what each session read (sessions.md R14, R15, R17).

A journal post carries ``meta.session``, the R9 id of the session it covers. It
defaults to the posting session. ``meta.cites`` lists the journals that shaped
the work, each with a one-line reason, and points only at journal posts. Every
journal that ``fetch`` hands a session lands in ``session_reads``, the candidate
list the agent picks its citations from at debrief.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from awm.gatewayclient import ids
from awm.journal.dao import JournalDAO
from awm.journal.refs import now_ms

MAX_REASON = 200

#: The kind that marks a journal post. Kinds are to become tags (fleet.md R107),
#: so every journal test in this module goes through ``is_journal``.
JOURNAL_KIND = "journal"


def is_journal(kind: Any) -> bool:
    return kind == JOURNAL_KIND


def r9(raw: Any) -> str | None:
    """The R9 form of a session id, or None when ``raw`` is not one."""
    return ids.to_r9(raw)


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
    session = r9(named)
    if session is None:
        raise ValueError(f"meta.session {named!r} is not a session id")
    meta["session"] = session
    if caller and caller != session:
        meta["posted_by"] = caller
    return meta


def check_cites(meta: Any, kind: str) -> Any:
    """``meta`` with ``cites`` checked: journal posts only, each with a one-line reason."""
    if not isinstance(meta, dict) or "cites" not in meta:
        return meta
    cites = meta["cites"]
    if cites in (None, []):
        return {k: v for k, v in meta.items() if k != "cites"}
    if not is_journal(kind):
        raise ValueError("only a journal post may cite (sessions.md R14)")
    if not isinstance(cites, list):
        raise ValueError("meta.cites is a list of {journal, reason}")
    out, seen = [], set()
    dao = JournalDAO()
    for c in cites:
        if not isinstance(c, dict) or not isinstance(c.get("journal"), str):
            raise ValueError("each citation is {journal: <post id>, reason: <one line>}")
        reason = c.get("reason")
        if not isinstance(reason, str) or not reason.strip() or "\n" in reason.strip():
            raise ValueError(f"citation {c['journal']} needs a one-line reason")
        row = dao.query_one("SELECT kind FROM scope_posts WHERE id=?", (c["journal"],))
        if row is None or not is_journal(row["kind"]):
            raise ValueError(f"citation {c['journal']} is not a journal post. Journal for that "
                             f"session first, then cite the new journal (sessions.md R17)")
        if c["journal"] not in seen:
            seen.add(c["journal"])
            out.append({"journal": c["journal"], "reason": reason.strip()[:MAX_REASON]})
    return {**meta, "cites": out}


def record_reads(session: str | None, posts: Iterable[Any]) -> int:
    """Note each journal in ``posts`` as read by ``session``. Its own journals are skipped."""
    if not session:
        return 0
    ids = [p.id for p in posts
           if is_journal(getattr(p, "kind", None)) and (p.meta or {}).get("session") != session]
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
    rows = JournalDAO().query_all(
        "SELECT id, meta FROM scope_posts WHERE kind=? AND json_extract(meta, '$.session')=?",
        (JOURNAL_KIND, session))
    out, seen = [], set()
    for r in rows:
        for c in _meta(r["meta"]).get("cites") or []:
            if isinstance(c, dict) and c.get("journal") not in seen:
                seen.add(c.get("journal"))
                out.append({**c, "from": r["id"]})
    if out:
        marks = ",".join("?" * len(out))
        cited = {r["id"]: _meta(r["meta"]).get("session") for r in JournalDAO().query_all(
            f"SELECT id, meta FROM scope_posts WHERE id IN ({marks})", [c["journal"] for c in out])}
        for c in out:
            c["session"] = cited.get(c["journal"])
    return out


def _meta(raw: Any) -> dict:
    try:
        meta = json.loads(raw or "{}")
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}
