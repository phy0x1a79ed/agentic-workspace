"""The lineage graph: who started each session, and what its debrief built on (sessions.md R13, R16).

A parent link is a hard edge, "started by"; reflection records one when it
starts a session or a session clears itself. A citation is a soft edge, "built
on": a debrief journal's ``reply_to`` list. Both point one way, and the walk
goes only forward: from a session to its parent and to the journals it cites,
then on from each journal's session.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from awm.journal import kinds, sessions
from awm.journal.dao import JournalDAO

PARENT_KINDS = ("start", "clear")
MAX_EDGES = 200


def set_parent(session_id: str, parent_id: str, how: str = "start", node: str = "",
               at: float | None = None) -> bool:
    """Record who started ``session_id``, or which session it was before a clear.

    The first link wins: a session has one parent. False when nothing changed.
    """
    if how not in PARENT_KINDS:
        raise ValueError(f"how is one of {PARENT_KINDS}, not {how!r}")
    session, parent = sessions.r9(session_id), sessions.r9(parent_id)
    if session is None or parent is None:
        raise ValueError("session_id and parent are session ids")
    if session == parent:
        return False
    return JournalDAO().execute(
        "INSERT OR IGNORE INTO session_parents (session, parent, how, node, at) VALUES (?,?,?,?,?)",
        (session, parent, how, node, at if at is not None else time.time())) > 0


def parent_of(session: str) -> dict | None:
    row = JournalDAO().query_one("SELECT * FROM session_parents WHERE session=?", (session,))
    return dict(row) if row else None


def cited_by_session(session: str) -> list[dict]:
    """The journals that ``session``'s own journals cite, with the session each covers."""
    rows = JournalDAO().query_all(
        "SELECT c.post_id, c.cited_id, json_extract(t.meta, '$.session') AS session"
        " FROM post_cites c JOIN scope_posts p ON p.id = c.post_id"
        " JOIN scope_posts t ON t.id = c.cited_id"
        " WHERE json_extract(p.meta, '$.session') = ? AND EXISTS (SELECT 1 FROM post_tags pt"
        " WHERE pt.post_id = p.id AND pt.tag_id = ?) ORDER BY p.ts, c.ord",
        (session, kinds.JOURNAL))
    return [{"journal": r["cited_id"], "session": r["session"], "from": r["post_id"]} for r in rows]


def walk(session_id: str, *, limit: int = MAX_EDGES) -> dict:
    """Every edge reachable forward from ``session_id``: parents, then what each debrief cites."""
    start = sessions.r9(session_id)
    if start is None:
        raise ValueError(f"{session_id!r} is not a session id")
    edges, seen, queue = [], {start}, [start]
    while queue and len(edges) < limit:
        s = queue.pop(0)
        p = parent_of(s)
        if p is not None:
            edges.append({"from": s, "to": p["parent"], "kind": "parent", "how": p["how"],
                          "node": p["node"], "at": p["at"]})
            if p["parent"] not in seen:
                seen.add(p["parent"])
                queue.append(p["parent"])
        for c in cited_by_session(s):
            edges.append({"from": s, "to": c["journal"], "kind": "cite", "via": c["from"],
                          "session": c["session"]})
            if c["session"] and c["session"] not in seen:
                seen.add(c["session"])
                queue.append(c["session"])
    return {"session": start, "edges": edges[:limit], "truncated": len(edges) >= limit}


def import_parents(source: Path) -> dict:
    """Copy reporting's ``session_parents`` rows into the journal once. The first link still wins."""
    if not source.exists():
        return {"status": "no-source", "source": str(source)}
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute("SELECT session, parent, how, node, at FROM session_parents").fetchall()
    except sqlite3.OperationalError:
        return {"status": "no-table", "source": str(source)}
    finally:
        src.close()
    made = sum(set_parent(r["session"], r["parent"], r["how"] if r["how"] in PARENT_KINDS
                          else "start", r["node"] or "", r["at"]) for r in rows)
    return {"status": "imported", "source": str(source), "rows": len(rows), "recorded": made}
