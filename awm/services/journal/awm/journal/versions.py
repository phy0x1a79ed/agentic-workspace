"""Edits to a post, kept as versions (fleet.md R107 items 1-2).

An edit stores the new body as the post's latest and keeps every earlier body
with the diff from the one before it, as git keeps commits. A read shows the
latest version. The addendum sweep folds a post that corrects an earlier one
into that post, as a new version, and retires the addendum from the reads.
"""

from __future__ import annotations

import difflib
import re

from awm.journal import channel, search_index
from awm.journal.dao import JournalDAO
from awm.journal.refs import ms_to_iso, now_ms

FOLDED_KIND = "folded"
ADDENDUM_RE = re.compile(r"^\s*(?:#+\s*)?(addendum|correction|erratum|update)\b[^\n]*?\(?\b([0-9a-f]{8})(?:[0-9a-f-]{0,28})\b",
                         re.IGNORECASE)


def _diff(old: str, new: str, post_id: str, version: int) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"{post_id}@{version - 1}", tofile=f"{post_id}@{version}"))


def edit(post_id: str, *, editor: str, body: str | None = None, old: str | None = None,
         new: str | None = None, reason: str = "") -> dict:
    """Store a new version of ``post_id``: a whole ``body``, or ``old`` replaced by ``new`` once."""
    row = JournalDAO().query_one("SELECT * FROM scope_posts WHERE id=?", (post_id,))
    if row is None:
        raise ValueError(f"no post {post_id}")
    current = row["body"] or ""
    if body is None:
        if old is None or new is None:
            raise ValueError("give body, or old and new")
        if current.count(old) != 1:
            raise ValueError("old must occur exactly once in the post")
        body = current.replace(old, new, 1)
    if body == current:
        raise ValueError("the edit changes nothing")
    version = int(row["version"] or 1) + 1
    now = now_ms()
    with JournalDAO().transaction() as conn:
        dao = JournalDAO(conn=conn)
        if version == 2:
            dao.execute("INSERT OR IGNORE INTO post_versions (post_id, version, body, diff, editor, reason, ts) "
                        "VALUES (?, 1, ?, '', ?, '', ?)", (post_id, current, row["author"], row["ts"]))
        dao.execute("INSERT INTO post_versions (post_id, version, body, diff, editor, reason, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (post_id, version, body, _diff(current, body, post_id, version), editor, reason, now))
        dao.execute("UPDATE scope_posts SET body=?, version=? WHERE id=?", (body, version, post_id))
    if row["kind"] in search_index.INDEXED_KINDS:
        search_index.index_post(post_id)
    return {"post": channel.get_post(post_id).to_dict(), "version": version}


def history(post_id: str) -> dict:
    """Every version of a post, oldest first, each with its diff from the one before."""
    post = channel.get_post(post_id)
    if post is None:
        raise ValueError(f"no post {post_id}")
    rows = JournalDAO().query_all("SELECT * FROM post_versions WHERE post_id=? ORDER BY version", (post_id,))
    if not rows:
        return {"post_id": post_id, "versions": [{"version": 1, "body": post.body, "diff": "",
                                                    "editor": post.author, "reason": "", "ts": post.ts}]}
    return {"post_id": post_id, "versions": [
        {"version": r["version"], "body": r["body"], "diff": r["diff"], "editor": r["editor"],
         "reason": r["reason"], "ts": ms_to_iso(r["ts"]) or ""} for r in rows]}


def addenda(project: str | None = None, scope: str | None = None) -> list[dict]:
    """Posts that open as an addendum or correction to an earlier post of the same
    scope, named by its id or the first 8 characters of it."""
    where, params = ["kind != ?"], [FOLDED_KIND]
    if project:
        where.append("owner_project=?")
        params.append(project)
    if scope:
        where.append("owner_scope=?")
        params.append(scope)
    rows = JournalDAO().query_all(
        f"SELECT id, owner_project, owner_scope, body, ts FROM scope_posts WHERE {' AND '.join(where)} ORDER BY ts",
        tuple(params))
    found = []
    for r in rows:
        m = ADDENDUM_RE.match(r["body"] or "")
        if not m:
            continue
        target = JournalDAO().query_one(
            "SELECT id FROM scope_posts WHERE owner_project=? AND owner_scope=? AND id LIKE ? AND id != ? "
            "AND ts < ? ORDER BY ts DESC LIMIT 1",
            (r["owner_project"], r["owner_scope"], m.group(2).lower() + "%", r["id"], r["ts"]))
        if target:
            found.append({"addendum": r["id"], "target": target["id"], "project": r["owner_project"],
                          "scope": r["owner_scope"], "first_line": (r["body"] or "").splitlines()[0][:160]})
    return found


def fold(addendum_id: str, target_id: str, *, editor: str) -> dict:
    """Append the addendum to its target as a new version, then retire the addendum."""
    add = channel.get_post(addendum_id)
    target = channel.get_post(target_id)
    if add is None or target is None:
        raise ValueError("no such post")
    stamp = add.ts[:10]
    body = f"{target.body.rstrip()}\n\n**Correction ({stamp})**\n\n{add.body.strip()}\n"
    out = edit(target_id, editor=editor, body=body, reason=f"fold addendum {addendum_id}")
    with JournalDAO().transaction() as conn:
        JournalDAO(conn=conn).execute(
            "UPDATE scope_posts SET kind=?, meta=json_set(meta, '$.folded_into', ?) WHERE id=?",
            (FOLDED_KIND, target_id, addendum_id))
    return out


def sweep(project: str | None = None, scope: str | None = None, *, apply: bool = False,
          editor: str = "system") -> dict:
    """List the addenda and their targets. With ``apply``, fold each one in."""
    found = addenda(project, scope)
    if apply:
        for f in found:
            fold(f["addendum"], f["target"], editor=editor)
    return {"addenda": found, "applied": bool(apply)}
