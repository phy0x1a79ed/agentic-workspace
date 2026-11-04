"""The tag vocabulary and the tags on posts (fleet.md R107 items 6 to 8).

A post links to a tag by the tag's id, so a rename shows on every post. The
tags the code names have fixed ids. The four kind tags replace the old ``kind``
column, and :func:`derived_kind` still answers ``kind`` for old peers and the
skills that read it.
"""

from __future__ import annotations

import re
import sqlite3
import uuid as _uuid
from dataclasses import dataclass
from typing import Any, Iterable

from awm.journal.dao import JournalDAO
from awm.journal.builtin_tags import (BRIEF, JOURNAL, KIND_TAGS, KIND_TO_TAG, MESSAGE,
                                      MIGRATED_PREFIX, PRECEDENT)
from awm.journal.refs import ms_to_iso, now_ms

_TAG_TO_KIND = {v: k for k, v in KIND_TO_TAG.items()}
#: Set when a post is made, never after (fleet.md R107 item 6).
TYPE_TAGS = (*KIND_TAGS, PRECEDENT)
_KIND_ORDER = (JOURNAL, BRIEF, PRECEDENT, MESSAGE)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.:+/-]{0,63}$")
_RESERVED = {"and", "not", "or"}


class TagError(ValueError):
    """A tag or a tag filter was refused."""


@dataclass
class Tag:
    id: str
    name: str
    description: str
    origin: str
    version: int
    created_ms: int
    updated_at: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                "origin": self.origin, "version": self.version, "created_ms": self.created_ms,
                "updated_at": self.updated_at}


def _row_to_tag(r) -> Tag:
    return Tag(r["id"], r["name"], r["description"], r["origin"] or "", r["version"],
               r["created_at"], ms_to_iso(r["updated_at"]) or "")


def normalize_name(name: Any) -> str:
    """The stored form of a tag name, or :class:`TagError` when it cannot be one."""
    text = " ".join(str(name or "").split()).lower()
    if not _NAME_RE.match(text) or text in _RESERVED:
        raise TagError(f"{name!r} is not a tag name: use lowercase letters, digits and "
                       "_ . : + / -, at most 64 characters, not and/or/not")
    return text


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

def vocabulary(conn: sqlite3.Connection | None = None) -> list[Tag]:
    rows = JournalDAO(conn=conn).query_all("SELECT * FROM tags ORDER BY name")
    return [_row_to_tag(r) for r in rows]


def _vocabulary_line() -> str:
    return ", ".join(t.name for t in vocabulary())


def get(tag_id: str) -> Tag | None:
    row = JournalDAO().query_one("SELECT * FROM tags WHERE id=?", (tag_id,))
    return _row_to_tag(row) if row else None


def by_name(name: str) -> Tag | None:
    row = JournalDAO().query_one("SELECT * FROM tags WHERE name=? COLLATE NOCASE",
                                 (" ".join(str(name).split()).lower(),))
    return _row_to_tag(row) if row else None


def _unknown(names: Iterable[str]) -> TagError:
    return TagError(f"unknown tag {', '.join(repr(n) for n in names)}. The vocabulary is: "
                    f"{_vocabulary_line()}. Add a tag with tag_add(name, description).")


def resolve(names: Iterable[str]) -> list[str]:
    """The ids of the named tags, in order, or :class:`TagError` naming each unknown one."""
    ids, missing = [], []
    for n in names:
        tag = by_name(n)
        if tag is None:
            missing.append(n)
        elif tag.id not in ids:
            ids.append(tag.id)
    if missing:
        raise _unknown(missing)
    return ids


def names_arg(raw: Any) -> list[str]:
    """A tag list argument as names: a list, or one comma-separated string."""
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    raise TagError("tags is a list of tag names")


def add(name: str, description: str, *, by: str = "", origin: str | None = None) -> Tag:
    """Add a tag to the vocabulary. The description is required."""
    name = normalize_name(name)
    description = (description or "").strip()
    if not description:
        raise TagError("a tag needs a description: say what a post with this tag is")
    if by_name(name) is not None:
        raise TagError(f"tag {name!r} exists already; change it with tag_edit")
    from awm.journal import tagsync
    if tagsync.provisional(name):
        raise TagError(f"{name!r} has the shape of a clash rename; choose another name")
    tid = _uuid.uuid4().hex
    now = now_ms()
    node = _node() if origin is None else origin
    dao = JournalDAO()
    with dao.transaction() as conn:
        d = JournalDAO(conn=conn)
        d.execute("INSERT INTO tags (id, name, description, origin, version, created_at, updated_at)"
                  " VALUES (?,?,?,?,1,?,?)", (tid, name, description, node, now, now))
        d.execute("INSERT INTO tag_versions (tag_id, version, name, description, edited_at, edited_by)"
                  " VALUES (?,1,?,?,?,?)", (tid, name, description, now, by))
    tag = get(tid)
    sync({"op": "add", "tag": tag.to_dict()})
    return tag


def edit(tag_ref: str, *, name: str | None = None, description: str | None = None,
         by: str = "") -> Tag:
    """Change a tag's name or description. The earlier version stays in ``tag_versions``."""
    tag = get(tag_ref) or by_name(tag_ref)
    if tag is None:
        raise _unknown([tag_ref])
    new_name = normalize_name(name) if name not in (None, "") else tag.name
    new_desc = (description or "").strip() if description not in (None, "") else tag.description
    if new_name == tag.name and new_desc == tag.description:
        return tag
    clash = by_name(new_name)
    if clash is not None and clash.id != tag.id:
        raise TagError(f"tag {new_name!r} exists already")
    now = now_ms()
    dao = JournalDAO()
    with dao.transaction() as conn:
        d = JournalDAO(conn=conn)
        if d.query_one("SELECT 1 FROM tag_versions WHERE tag_id=?", (tag.id,)) is None:
            # A fixed or migrated tag has no first version row until its first edit.
            d.execute("INSERT INTO tag_versions (tag_id, version, name, description, edited_at,"
                      " edited_by) VALUES (?,?,?,?,?,?)",
                      (tag.id, tag.version, tag.name, tag.description, 0, ""))
        version = tag.version + 1
        d.execute("UPDATE tags SET name=?, description=?, version=?, updated_at=? WHERE id=?",
                  (new_name, new_desc, version, now, tag.id))
        d.execute("INSERT INTO tag_versions (tag_id, version, name, description, edited_at, edited_by)"
                  " VALUES (?,?,?,?,?,?)", (tag.id, version, new_name, new_desc, now, by))
    out = get(tag.id)
    from awm.journal import tagsync
    tagsync.resolved_by_edit(out.id)
    sync({"op": "edit", "tag": out.to_dict()})
    return out


def versions(tag_ref: str) -> list[dict]:
    tag = get(tag_ref) or by_name(tag_ref)
    if tag is None:
        raise _unknown([tag_ref])
    rows = JournalDAO().query_all(
        "SELECT version, name, description, edited_at, edited_by FROM tag_versions"
        " WHERE tag_id=? ORDER BY version", (tag.id,))
    return [{"version": r["version"], "name": r["name"], "description": r["description"],
             "edited_at": ms_to_iso(r["edited_at"]) if r["edited_at"] else None,
             "edited_by": r["edited_by"] or None} for r in rows]


def sync(change: dict) -> None:
    """Send a vocabulary change to the other nodes (fleet.md R107 item 6)."""
    from awm.journal import tagsync
    tagsync.push(change)


def _node() -> str:
    try:
        from awm import config
        return config.node_name()
    except Exception:  # noqa: BLE001 — the origin is a label, never a reason to refuse
        return ""


# ---------------------------------------------------------------------------
# Tags on posts
# ---------------------------------------------------------------------------

def of_posts(post_ids: list[str], conn: sqlite3.Connection | None = None) -> dict[str, list[str]]:
    """Each post's tag ids, in the order :data:`_KIND_ORDER` then by name."""
    if not post_ids:
        return {}
    out: dict[str, list[str]] = {pid: [] for pid in post_ids}
    dao = JournalDAO(conn=conn)
    for i in range(0, len(post_ids), 500):
        chunk = post_ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in dao.query_all(
                f"SELECT pt.post_id, pt.tag_id FROM post_tags pt JOIN tags t ON t.id = pt.tag_id"
                f" WHERE pt.post_id IN ({marks}) ORDER BY t.name", chunk):
            out[r["post_id"]].append(r["tag_id"])
    return out


def names_of(tag_ids: Iterable[str]) -> list[str]:
    ids = list(tag_ids)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    by_id = {r["id"]: r["name"] for r in JournalDAO().query_all(
        f"SELECT id, name FROM tags WHERE id IN ({marks})", ids)}
    return [by_id[i] for i in ids if i in by_id]


def derived_kind(tag_ids: Iterable[str]) -> str:
    """The ``kind`` an old reader expects: the first kind tag, else a migrated kind, else message."""
    ids = set(tag_ids)
    for t in _KIND_ORDER:
        if t in ids:
            return _TAG_TO_KIND[t]
    for t in sorted(ids):
        if t.startswith(MIGRATED_PREFIX):
            return t[len(MIGRATED_PREFIX):]
    return "message"


def attach(conn: sqlite3.Connection, post_id: str, tag_ids: Iterable[str]) -> None:
    JournalDAO(conn=conn).executemany(
        "INSERT OR IGNORE INTO post_tags (post_id, tag_id) VALUES (?, ?)",
        [(post_id, t) for t in tag_ids])


def detach(conn: sqlite3.Connection, post_id: str, tag_ids: Iterable[str]) -> None:
    JournalDAO(conn=conn).executemany(
        "DELETE FROM post_tags WHERE post_id=? AND tag_id=?", [(post_id, t) for t in tag_ids])


def has(post_id: str, tag_id: str, conn: sqlite3.Connection | None = None) -> bool:
    return JournalDAO(conn=conn).query_one(
        "SELECT 1 FROM post_tags WHERE post_id=? AND tag_id=?", (post_id, tag_id)) is not None


def retag(post_id: str, *, add: Iterable[str] = (), remove: Iterable[str] = ()) -> list[str]:
    """Add and remove several tags on one post in one transaction; returns its tag ids.

    A type tag (message, journal, brief, precedent) is set only when the
    post is made (fleet.md R107 item 6); closed and superseded change freely.
    """
    add_ids, remove_ids = resolve(add), resolve(remove)
    both = set(add_ids) & set(remove_ids)
    if both:
        raise TagError(f"tag {', '.join(names_of(both))} is both added and removed")
    fixed = [t for t in add_ids + remove_ids if t in TYPE_TAGS]
    if fixed:
        raise TagError(f"{', '.join(names_of(fixed))} is set when a post is written and "
                       "cannot change after it")
    dao = JournalDAO()
    with dao.transaction() as conn:
        attach(conn, post_id, add_ids)
        detach(conn, post_id, remove_ids)
    return of_posts([post_id])[post_id]


# ---------------------------------------------------------------------------
# Filters: conjunction and negation, such as "!closed and complaint"
# ---------------------------------------------------------------------------

def parse_filter(expr: Any) -> list[tuple[bool, str]]:
    """``[(negated, name), ...]`` from ``"a and !b"``, or from a list of such terms."""
    if expr in (None, "", []):
        return []
    parts = expr if isinstance(expr, (list, tuple)) else re.split(r"\s+and\s+", str(expr).strip(),
                                                                  flags=re.I)
    terms = []
    for raw in parts:
        term = str(raw).strip()
        negated = False
        while True:
            if term.startswith("!"):
                negated, term = not negated, term[1:].strip()
            elif term.lower().startswith("not "):
                negated, term = not negated, term[4:].strip()
            else:
                break
        if not term or " " in term:
            raise TagError(f"cannot read the tag filter {expr!r}: join tags with 'and', "
                           "negate one with '!', such as '!closed and complaint'")
        terms.append((negated, term))
    return terms


def filter_sql(expr: Any, *, column: str = "scope_posts.id") -> tuple[str, list]:
    """A WHERE fragment, ``(" AND …", params)``, keeping the posts that match ``expr``."""
    terms = parse_filter(expr)
    if not terms:
        return "", []
    found = {n: by_name(n) for _, n in terms}
    missing = [n for n, t in found.items() if t is None]
    if missing:
        raise _unknown(missing)
    sql, params = "", []
    for negated, name in terms:
        sql += (f" AND {'NOT ' if negated else ''}EXISTS (SELECT 1 FROM post_tags pt"
                f" WHERE pt.post_id = {column} AND pt.tag_id = ?)")
        params.append(found[name].id)
    return sql, params
