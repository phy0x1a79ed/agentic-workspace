"""A scope's inbox: a bounded digest of its channel's posts and the scope's read cursor.

The cursor belongs to the scope, not to a session. Seats churn: a scope outlives
many sessions, and the session that arrives next must not re-read what the last
one already handled. A post the scope wrote itself is never unread.

The cursor is a position ``(ts, id)`` in the channel, so it is the same order the
digest lists in. Acknowledging through a post marks it and everything before it
read. The cursor only moves forward.
"""

from __future__ import annotations

import re

from awm.journal.channel import _author_to_display, _coerce_meta
from awm.journal.dao import JournalDAO
from awm.journal.refs import ms_to_iso, now_ms

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
GIST_WIDTH = 120

_TAG_RUN_RE = re.compile(r"<[^>\n]*>")
_GREETING_RE = re.compile(r"^\s*hi,? this is claude on behalf of tony[.,!:]?\s*", re.I)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")
_ASK_RE = re.compile(
    r"\?|\b(please|request(?:s|ed)?|can you|could you|would you|needs? you|"
    r"action required|todo|blocked|bug|broken|fix(?:es)?)\b", re.I)
_ASK_TYPES = {"request", "bug", "question", "blocked"}


def _one_line(text: str, width: int = GIST_WIDTH) -> str:
    text = re.sub(r"\s+", " ", _TAG_RUN_RE.sub("", text or "")).strip()
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def gist(body: str, meta: dict) -> str:
    """One line for a post: its ``meta.title``, else a leading heading, else its first sentence.

    The courtesy greeting that opens a post sent on Tony's behalf carries nothing
    and is dropped first.
    """
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return _one_line(title)
    text = _GREETING_RE.sub("", body or "")
    paragraph = text.strip().split("\n\n", 1)[0]
    first = paragraph.strip().splitlines()[0] if paragraph.strip() else ""
    heading = _HEADING_RE.match(first)
    if heading:
        return _one_line(heading.group(1))
    return _one_line(_SENTENCE_END_RE.split(" ".join(paragraph.split()), 1)[0])


def asks(body: str, meta: dict, line: str) -> bool:
    """Whether a post looks like it asks the reader to act. A keyword heuristic, not a judgement."""
    if str(meta.get("type", "")).lower() in _ASK_TYPES:
        return True
    return bool(_ASK_RE.search(f"{line} {(body or '')[:600]}"))


def _age(ts_ms: int, now: int) -> str:
    minutes = max(0, now - ts_ms) // 60000
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def cursor(project: str, scope: str) -> dict | None:
    row = JournalDAO().query_one(
        "SELECT * FROM scope_cursors WHERE owner_project=? AND owner_scope=?", (project, scope))
    if row is None:
        return None
    return {"through_id": row["through_id"], "through_ts": ms_to_iso(row["through_ts"]),
            "acked_at": ms_to_iso(row["acked_at"]), "acked_by": row["acked_by"] or None}


def _unread_clause(project: str, scope: str) -> tuple[str, list]:
    """A WHERE fragment keeping posts after the scope's cursor and not by the scope itself."""
    sql = " AND author != ?"
    params: list = [f"agent:{project}/{scope}"]
    row = JournalDAO().query_one(
        "SELECT through_ts, through_id FROM scope_cursors WHERE owner_project=? AND owner_scope=?",
        (project, scope))
    if row is not None:
        sql += " AND (ts > ? OR (ts = ? AND id > ?))"
        params += [row["through_ts"], row["through_ts"], row["through_id"]]
    return sql, params


def unread_count(project: str, scope: str, *, kind: str = "message",
                 exclude: tuple[str, list] = ("", [])) -> int:
    unread, unread_params = _unread_clause(project, scope)
    return JournalDAO().query_one(
        "SELECT COUNT(*) AS n FROM scope_posts WHERE owner_project=? AND owner_scope=? "
        f"AND kind=?{exclude[0]}{unread}",
        (project, scope, kind, *exclude[1], *unread_params))["n"]


def digest(project: str, scope: str, *, kind: str = "message", limit: int = DEFAULT_LIMIT,
           include_read: bool = False, exclude: tuple[str, list] = ("", [])) -> dict:
    """The newest ``limit`` unread posts of one channel, one line each, plus the counts around them.

    ``include_read`` lists read posts too, each marked ``read``. ``ack_through`` is the
    newest unread post listed: pass it to :func:`ack` so a post that arrives after
    this digest stays unread.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    dao = JournalDAO()
    base = "FROM scope_posts WHERE owner_project=? AND owner_scope=? AND kind=?" + exclude[0]
    base_params = [project, scope, kind, *exclude[1]]
    unread_sql, unread_params = _unread_clause(project, scope)
    total = dao.query_one(f"SELECT COUNT(*) AS n {base}", base_params)["n"]
    unread = dao.query_one(f"SELECT COUNT(*) AS n {base}{unread_sql}",
                           [*base_params, *unread_params])["n"]
    unread_ids = None
    if include_read:
        rows = dao.query_all(f"SELECT * {base} ORDER BY ts DESC, id DESC LIMIT ?",
                             [*base_params, limit])
        unread_ids = {r["id"] for r in dao.query_all(
            f"SELECT id {base}{unread_sql}", [*base_params, *unread_params])}
    else:
        rows = dao.query_all(f"SELECT * {base}{unread_sql} ORDER BY ts DESC, id DESC LIMIT ?",
                             [*base_params, *unread_params, limit])

    now = now_ms()
    posts = []
    for r in rows:
        meta = _coerce_meta(r["meta"])
        line = gist(r["body"], meta)
        item = {"id": r["id"], "author": _author_to_display(r["author"]),
                "age": _age(r["ts"], now), "gist": line,
                "asks": asks(r["body"], meta, line), "chars": len(r["body"] or "")}
        if unread_ids is not None:
            item["read"] = r["id"] not in unread_ids
        posts.append(item)

    listed_unread = [p for p in posts if not p.get("read")]
    out = {
        "project": project, "scope": scope, "kind": kind,
        "unread": unread, "total": total, "posts": posts,
        "more": (total if include_read else unread) - len(posts),
        "cursor": cursor(project, scope),
        "ack_through": listed_unread[0]["id"] if listed_unread else None,
    }
    if posts:
        out["next"] = ("Read a post in full with fetch(post_id=<id>). Mark these read with "
                       "ack(project, scope, through=<ack_through>).")
    return out


def ack(project: str, scope: str, *, through: str | None = None, by: str | None = None) -> dict:
    """Move the scope's cursor to ``through``, a post of its channel, or to its newest post.

    A ``through`` older than the cursor leaves the cursor where it is.
    """
    dao = JournalDAO()
    if through:
        row = dao.query_one(
            "SELECT id, ts FROM scope_posts WHERE id=? AND owner_project=? AND owner_scope=?",
            (through, project, scope))
        if row is None:
            raise ValueError(f"no post {through} in {project}/{scope}")
    else:
        row = dao.query_one(
            "SELECT id, ts FROM scope_posts WHERE owner_project=? AND owner_scope=? "
            "ORDER BY ts DESC, id DESC LIMIT 1", (project, scope))
    moved = False
    if row is not None:
        with dao.transaction() as conn:
            current = JournalDAO(conn=conn).query_one(
                "SELECT through_ts, through_id FROM scope_cursors "
                "WHERE owner_project=? AND owner_scope=?", (project, scope))
            if current is None or (row["ts"], row["id"]) > (current["through_ts"], current["through_id"]):
                JournalDAO(conn=conn).execute(
                    "INSERT INTO scope_cursors "
                    "(owner_project, owner_scope, through_ts, through_id, acked_at, acked_by) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(owner_project, owner_scope) DO UPDATE SET "
                    "through_ts=excluded.through_ts, through_id=excluded.through_id, "
                    "acked_at=excluded.acked_at, acked_by=excluded.acked_by",
                    (project, scope, row["ts"], row["id"], now_ms(), by or ""))
                moved = True
    return {"project": project, "scope": scope, "moved": moved,
            "cursor": cursor(project, scope), "unread": unread_count(project, scope)}
