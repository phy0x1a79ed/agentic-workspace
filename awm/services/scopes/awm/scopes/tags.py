"""Tags on projects, scopes and scope groups.

A scope's effective tags are those of the nearest level that has any: the
scope, then each enclosing group, then the project. A nearer set replaces a
farther one; the levels never merge. Tags label and filter, and gate nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any

from awm.scopes._validation import validate_name, validate_scope_name
from awm.scopes.dao import ScopesDAO
from awm.scopes.identity import now_ms

RULE = ("a tag is lowercase letters, digits, '.', '_' or '-', starts with a "
        "letter or digit, and is at most 32 characters")
_TAG = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")

Table = dict[tuple[str, str], list[str]]


def normalise(raw: Any) -> list[str]:
    """The sorted, deduplicated tags in ``raw``: a list, a JSON list or a comma string."""
    if isinstance(raw, str):
        text = raw.strip()
        raw = json.loads(text) if text.startswith("[") else text.split(",")
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"tags must be a list or a comma-separated string, not {raw!r}")
    out: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"tag {item!r} is not a string: {RULE}")
        tag = item.strip().lower()
        if not tag:
            continue
        if not _TAG.fullmatch(tag):
            raise ValueError(f"invalid tag {tag!r}: {RULE}")
        out.add(tag)
    return sorted(out)


def load(project: str | None = None) -> Table:
    """Every level's own tags, keyed by ``(project, scope path)``; ``''`` is the project level."""
    sql, params = "SELECT project, scope, tags FROM tags", ()
    if project is not None:
        sql, params = sql + " WHERE project=?", (project,)
    return {(r["project"], r["scope"]): json.loads(r["tags"])
            for r in ScopesDAO().query_all(sql, params)}


def levels(scope: str | None) -> list[str]:
    """The levels a scope inherits from, nearest first, ending with the project's ``''``."""
    parts = scope.split("/") if scope else []
    return ["/".join(parts[:i]) for i in range(len(parts), 0, -1)] + [""]


def nearest(table: Table, project: str, scope: str | None) -> tuple[str | None, list[str]]:
    """The nearest level with tags and its tags, or ``(None, [])`` when no level has any."""
    for level in levels(scope):
        found = table.get((project, level))
        if found:
            return level, list(found)
    return None, []


def resolve(table: Table, project: str, scope: str | None) -> list[str]:
    """A scope's effective tags, or the project's when ``scope`` is empty."""
    return nearest(table, project, scope)[1]


def effective(project: str, scope: str | None = None) -> list[str]:
    return resolve(load(project), project, scope)


def describe(project: str, scope: str | None = None) -> dict:
    """A level's own tags beside the effective tags and the level they come from."""
    table = load(project)
    level, found = nearest(table, project, scope)
    source = None if level is None else (f"{project}/{level}" if level else project)
    return {"project": project, "scope": scope or None,
            "tags": table.get((project, scope or ""), []),
            "effective": found, "effective_from": source}


def set_tags(project: str, scope: str | None, tags: Any) -> dict:
    """Replace a level's own tags; an empty set clears the level so it inherits."""
    validate_name(project, kind="project name")
    if scope:
        validate_scope_name(scope)
    clean = normalise(tags)
    key = (project, scope or "")
    dao = ScopesDAO()
    if clean:
        dao.execute(
            "INSERT INTO tags (project, scope, tags, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project, scope) DO UPDATE SET tags=excluded.tags, "
            "updated_at=excluded.updated_at",
            (*key, json.dumps(clean), now_ms()))
    else:
        dao.execute("DELETE FROM tags WHERE project=? AND scope=?", key)
    return describe(project, scope)


def drop(project: str, scope: str) -> None:
    """Forget a scope's own tags, leaving its groups' and project's."""
    ScopesDAO().execute("DELETE FROM tags WHERE project=? AND scope=?", (project, scope))

