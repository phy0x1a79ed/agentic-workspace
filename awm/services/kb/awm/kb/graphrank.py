"""PageRank for search hits, from the graph the kb project's ``kb.graph`` builds and ships beside the paper base.

A hit is a paper (source ``kb``, its id a paper id) or a search document (node ``doc:<source>:<id>``). A context
names a scope (``project/scope``) or a project. A scope's vector blends with its project's, and its own share
grows with its document count. A hit not yet in the graph takes its scope's mean.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = 1
#: A scope with this many documents weighs as much as its project.
OWN_SHARE_HALF = 20


def node_key(hit: dict[str, Any]) -> str:
    return f"paper:{hit['id']}" if hit.get("source") == "kb" else f"doc:{hit.get('source')}:{hit['id']}"


def profiles(context: str) -> tuple[str, str]:
    """The (scope, project) profile names a context names; either may be empty."""
    project, _, scope = context.strip().strip("/").partition("/")
    return (f"scope:{project}/{scope}" if project and scope else "", f"project:{project}" if project else "")


class Ranks:
    def __init__(self, path: Path):
        self.path = path
        self._db: sqlite3.Connection | None = None
        self._stamp: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection | None:
        """The shipped graph, reopened when a ship replaces the file; None when there is none to read."""
        try:
            st = self.path.stat()
        except OSError:
            return None
        stamp = (st.st_ino, st.st_mtime_ns)
        if self._db is None or stamp != self._stamp:
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            if not row or int(row[0]) != SCHEMA:
                db.close()
                return None
            if self._db is not None:
                self._db.close()
            self._db, self._stamp = db, stamp
        return self._db

    def status(self) -> dict[str, Any]:
        with self._lock:
            db = self._conn()
            if db is None:
                return {"graph": None}
            meta = dict(db.execute("SELECT key, value FROM meta"))
            return {"graph": str(self.path), "built": meta.get("built"),
                    "profiles": db.execute("SELECT count(*) FROM profile").fetchone()[0]}

    def score(self, hits: list[dict[str, Any]], context: str = "") -> dict[str, Any]:
        """``{"profile", "scores"}``: one score per hit, from the context's blended profile or the global one."""
        with self._lock:
            db = self._conn()
            if db is None:
                return {"profile": None, "scores": [None] * len(hits)}
            return self._score(db, hits, context)

    def _score(self, db: sqlite3.Connection, hits: list[dict[str, Any]], context: str) -> dict[str, Any]:
        rows = {r[0]: r[1:] for r in db.execute("SELECT name, seeds, floor, mean, gmean FROM profile")}
        scope, project = profiles(context)
        weights = {}
        if scope in rows and project in rows:
            own = rows[scope][0] / (rows[scope][0] + OWN_SHARE_HALF)
            weights = {scope: own, project: 1.0 - own}
        elif scope in rows or project in rows:
            weights = {scope if scope in rows else project: 1.0}
        named = " + ".join(weights) or "global"
        keys = [node_key(h) for h in hits]
        nids = dict(db.execute(f"SELECT key, nid FROM node WHERE key IN ({','.join('?' * len(keys))})", keys)) \
            if keys else {}
        known = sorted(set(nids.values()))
        marks = ",".join("?" * len(known))

        def vector(profile: str) -> dict[int, float]:
            return dict(db.execute(f"SELECT nid, score FROM rank WHERE profile = ? AND nid IN ({marks})",
                                   [profile, *known])) if known else {}

        scores: list[float] = []
        if not weights:
            ranks = vector("global")
            for hit, key in zip(hits, keys):
                if key in nids:
                    scores.append(ranks.get(nids[key], rows["global"][1]))
                else:
                    home = rows.get(profiles(f"{hit.get('project') or ''}/{hit.get('scope') or ''}")[0])
                    scores.append(home[3] if home else rows["global"][2])
            return {"profile": named, "scores": scores}
        parts = {p: vector(p) for p in weights}
        for hit, key in zip(hits, keys):
            total = 0.0
            for p, w in weights.items():
                _, floor, mean, _ = rows[p]
                if key in nids:
                    total += w * parts[p].get(nids[key], floor)
                else:
                    mine = p in profiles(f"{hit.get('project') or ''}/{hit.get('scope') or ''}")
                    total += w * (mean if mine else floor)
            scores.append(total)
        return {"profile": named, "scores": scores}
