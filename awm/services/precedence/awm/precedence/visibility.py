"""Which decisions a caller may see: a peer never sees one tagged with a privately marked project.

A decision carries no project of its own. A tag equal to a marked project's name,
ignoring case, ties it to that project. A decision that only mentions the
project in its text stays visible.
"""

from __future__ import annotations

import sqlite3

from awm.config import privacy


def _name(text: str) -> str:
    return " ".join(str(text).split()).lower()


def hidden_ids(conn: sqlite3.Connection, as_: str | None) -> frozenset[str]:
    """The decision ids the caller may not see: none for a local caller.

    An unreadable marks file hides every tagged decision from a peer.
    """
    if not privacy.from_peer(as_):
        return frozenset()
    try:
        names: set[str] | None = {_name(m["project"]) for m in privacy.marks()}
    except privacy.MarksUnreadable:
        names = None
    if names is not None and not names:
        return frozenset()
    rows = conn.execute("SELECT decision_id, tag FROM tags").fetchall()
    return frozenset(r[0] for r in rows if names is None or _name(r[1]) in names)
